"""ADS device-notification subscription — connection pool + thread-safe callback.

Implements push-mode delivery for the ADS driver.  pyads invokes
device-notification callbacks on a **worker thread**, so the callback must only
enqueue data and hand it to the owning event loop — it must never touch the ADS
API, the coroutine result path, or the driver's state directly.

The subscription owns a pool of ``pyads.Connection`` instances.  Each connection
carries at most ``max_notifications_per_connection`` handles; once a connection
fills up, an additional connection is opened.  :meth:`subscribe` registers the
set difference against the currently-subscribed points, so repeated calls (e.g.
hot-reload) add/remove only what changed.

Note: pyads' real notification callback is a ctypes ``Notification`` structure;
parsing it back to ``(handle, name, timestamp, value)`` is deferred.  This module
defines the thread-safe delivery boundary and is exercised through a mock that
invokes the callback with those four arguments.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from wind_hub.adapter.outbound.protocol.ads.config import ADSConfig
from wind_hub.adapter.outbound.protocol.ads.mapping import ADSPoint
from wind_hub.domain.model.point import PointValue, Quality


def _pyads() -> Any:
    """Return the ``pyads`` module, imported lazily (it ships no ``py.typed``)."""
    import pyads  # type: ignore[import-untyped]

    return pyads


class ADSSubscription:
    """A pool of ADS connections delivering spontaneous value updates.

    Args:
        config: Resolved :class:`ADSConfig` (subscription + connection params).
        device_id: Device identifier stamped onto every pushed :class:`PointValue`.
        host: Target IP address for the subscription connections.
        loop: The owning event loop (the one ``on_data`` runs on).
        on_data: Async callback invoked with each pushed :class:`PointValue`.
    """

    def __init__(
        self,
        config: ADSConfig,
        device_id: str,
        host: str,
        loop: asyncio.AbstractEventLoop,
        on_data: Callable[[PointValue], Awaitable[None]],
        cycle_time: float,
    ) -> None:
        self._config = config
        self._device_id = device_id
        self._host = host
        self._loop = loop
        self._on_data = on_data
        # Notification cycle time（秒）——来自 Task.interval，由调用方在
        # 订阅建立时传入；不再是设备级配置。
        self._cycle_time = cycle_time

        # symbol name → resolved point
        self._points: dict[str, ADSPoint] = {}
        # open pyads connections and their current handle counts (parallel lists)
        self._connections: list[Any] = []
        self._loads: list[int] = []
        # symbol name → (connection index, notification handle, user handle)
        self._handles: dict[str, tuple[int, Any, Any]] = {}

        # Thread-safe hand-off from the pyads callback thread to the event loop.
        self._queue: queue.Queue[PointValue] = queue.Queue()
        self._closed = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def subscribe(self, points: list[ADSPoint]) -> None:
        """Register *points*, adding/removing only the set difference.

        Points without a symbol are skipped (device notifications require
        symbol addressing).
        """
        new_points: dict[str, ADSPoint] = {ap.symbol: ap for ap in points if ap.symbol is not None}
        current = set(self._points)

        added = [new_points[s] for s in new_points if s not in current]
        removed = [s for s in current if s not in new_points]

        if added:
            await self._register(added)
        if removed:
            await self._unregister(removed)

        self._points = new_points

    async def unsubscribe_all(self) -> None:
        """Unregister every currently subscribed point."""
        await self._unregister(list(self._points))
        self._points = {}

    async def close(self) -> None:
        """Unregister all notifications and close every pooled connection."""
        self._closed = True
        await self._unregister(list(self._points))
        self._points = {}
        for conn in self._connections:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(conn.close)
        self._connections = []
        self._loads = []

    # ------------------------------------------------------------------
    # connection pool / registration
    # ------------------------------------------------------------------

    def _connection_for(self) -> int:
        """Index of the first connection with spare handle capacity, or ``-1``."""
        max_per = self._config.max_notifications_per_connection
        for idx, load in enumerate(self._loads):
            if load < max_per:
                return idx
        return -1

    async def _create_connection(self) -> Any:
        """Open a new pyads connection on a worker thread."""
        pyads = _pyads()
        net_id = self._config.target_net_id or None
        conn = pyads.Connection(net_id, self._config.target_port, self._host)
        conn.set_timeout(int(self._config.timeout * 1000))
        await asyncio.to_thread(conn.open)
        return conn

    async def _register(self, points: list[ADSPoint]) -> None:
        """Register a device notification for each point, splitting connections."""
        pyads = _pyads()
        for ap in points:
            symbol = ap.symbol
            assert symbol is not None  # subscribe() pre-filters symbolless points
            idx = self._connection_for()
            if idx < 0:
                conn = await self._create_connection()
                self._connections.append(conn)
                self._loads.append(0)
                idx = len(self._connections) - 1
            conn = self._connections[idx]
            attr = pyads.NotificationAttrib(
                max(ap.size, 1),
                cycle_time=self._cycle_time,
                max_delay=self._config.max_delay,
            )
            handle, user_handle = await asyncio.to_thread(
                conn.add_device_notification, symbol, attr, self._on_notification
            )
            self._loads[idx] += 1
            self._handles[symbol] = (idx, handle, user_handle)

    async def _unregister(self, symbols: list[str]) -> None:
        """Delete the notifications for *symbols* and release their handle slots."""
        for symbol in symbols:
            if symbol not in self._handles:
                continue
            idx, handle, user_handle = self._handles.pop(symbol)
            conn = self._connections[idx]
            with contextlib.suppress(Exception):
                await asyncio.to_thread(conn.del_device_notification, handle, user_handle)
            self._loads[idx] = max(0, self._loads[idx] - 1)

    # ------------------------------------------------------------------
    # thread-safe callback → event loop
    # ------------------------------------------------------------------

    def _on_notification(self, handle: Any, name: str, timestamp: Any, value: Any) -> None:
        """pyads notification callback (runs on pyads' worker thread).

        Only enqueues a :class:`PointValue` and schedules a drain on the owning
        loop — no ADS API, coroutine, or driver state is touched here.
        """
        ap = self._points.get(name)
        if ap is None:
            return
        if timestamp is None:
            timestamp = datetime.now(UTC)
        pv = PointValue(
            device_id=self._device_id,
            point_id=ap.point_id,
            value=value,
            quality=Quality.GOOD,
            timestamp=timestamp,
            source="ads",
        )
        self._queue.put(pv)
        self._loop.call_soon_threadsafe(self._schedule_drain)

    def _schedule_drain(self) -> None:
        """Schedule a drain coroutine on the event loop (thread-safe entry)."""
        if self._closed:
            return
        asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """Deliver all queued values to ``on_data`` on the event loop."""
        while True:
            try:
                pv = self._queue.get_nowait()
            except queue.Empty:
                return
            await self._on_data(pv)
