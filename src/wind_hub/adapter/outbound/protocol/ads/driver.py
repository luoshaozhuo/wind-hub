"""ADS (Automation Device Specification) protocol driver over pyads.

Implements :class:`~wind_hub.domain.port.outbound.ProtocolPort` for the ADS
protocol.  Points may be addressed by ``index_group`` + ``index_offset`` or by
PLC symbol name (see :mod:`wind_hub.adapter.outbound.protocol.ads.mapping`).

Batch reads dispatch on ``read_mode``: ``'sum'`` packs many points into a single
ADS Sum command via symbol addressing (``read_list_by_name``), while
``'sequential'`` issues one per-point ``Read`` with a concurrency limit.
Device-notification subscription (push mode) is supported through
:mod:`wind_hub.adapter.outbound.protocol.ads.subscription` when
``subscribe.enabled`` is set.

pyads exposes a *synchronous* API, so every blocking call is offloaded to a
worker thread via :func:`asyncio.to_thread` to avoid stalling the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from wind_hub.adapter.outbound.protocol.ads.config import ADSConfig, from_device_config
from wind_hub.adapter.outbound.protocol.ads.mapping import ADSPoint, parse_point
from wind_hub.adapter.outbound.protocol.ads.subscription import ADSSubscription
from wind_hub.config.schema import DeviceConfig, PointConfig
from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import ProtocolError
from wind_hub.domain.model.point import PointRef, PointValue, Quality
from wind_hub.domain.port.outbound import HealthStatus, ProtocolPort

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_MULTIPLIER = 2.0


def _pyads() -> Any:
    """Return the ``pyads`` module, importing it lazily.

    pyads ships no ``py.typed`` marker, so it is untyped; importing it here
    (rather than at module top) also keeps the base package importable without
    the optional ``ads`` extra.
    """
    import pyads  # type: ignore[import-untyped]

    return pyads


def _plc_datatype(ads_name: str) -> Any:
    """Return the pyads ``PLCTYPE_*`` class for an ADS type name."""
    pyads = _pyads()
    return getattr(pyads, f"PLCTYPE_{ads_name}")


class ADSDriver:
    """ADS protocol driver (polling).

    Not thread-safe; a single asyncio event loop owns each instance.  An
    internal :class:`asyncio.Lock` serialises ``read``/``write`` calls.
    """

    def __init__(self, cfg: DeviceConfig) -> None:
        self._cfg = cfg
        self._config: ADSConfig = from_device_config(cfg)
        self._host = cfg.endpoint.host
        self._lock = asyncio.Lock()

        self._points: dict[str, ADSPoint] = {}
        self._connected = False
        self._failed = False
        self._shutdown = False

        self._reconnect_event = asyncio.Event()
        self._monitor_task: asyncio.Task[None] | None = None

        # pyads Connection, created lazily on connect (untyped on purpose so the
        # third-party type never leaks into this module's API).
        self._connection: Any = None

        # Device-notification subscription, created lazily on first subscribe.
        self._subscription: ADSSubscription | None = None

    # ------------------------------------------------------------------
    # point mapping
    # ------------------------------------------------------------------

    def set_points_mapping(self, points: list[PointConfig]) -> None:
        """Resolve a point table to :class:`ADSPoint` entries (last one wins)."""
        mapping: dict[str, ADSPoint] = {}
        for point in points:
            mapping[point.point_id] = parse_point(point)
        self._points = mapping

    # ------------------------------------------------------------------
    # ProtocolPort — connect / close
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the ADS connection, retrying with exponential backoff."""
        async with self._lock:
            if self._connected:
                return
            self._shutdown = False
            last_exc = await self._connect_with_retry()
            if last_exc is not None:
                raise ProtocolError(
                    f"ADS: failed to connect to {self._host} "
                    f"after {self._config.reconnect_max_retries + 1} attempts: {last_exc}"
                ) from last_exc
            self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def close(self) -> None:
        """Close the connection and stop the reconnect monitor."""
        async with self._lock:
            self._shutdown = True
            self._reconnect_event.set()
            if self._monitor_task is not None and not self._monitor_task.done():
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._monitor_task
            self._monitor_task = None
            if self._subscription is not None:
                await self._subscription.close()
                self._subscription = None
            self._close_connection()
            self._connected = False
            self._failed = False

    async def _connect_with_retry(self) -> Exception | None:
        """Attempt connection with exponential backoff.

        Returns:
            ``None`` on success, or the last exception once the retry budget is
            exhausted (after which ``self._failed`` is set).
        """
        backoff = _RECONNECT_BACKOFF_BASE
        last_exc: Exception | None = None
        budget = self._config.reconnect_max_retries + 1
        for attempt in range(budget):
            try:
                await self._do_connect()
                self._connected = True
                self._failed = False
                logger.info(
                    "ADS: connected to %s (net id %s)", self._host, self._config.target_net_id
                )
                return None
            except Exception as exc:  # connection failure — retry with backoff
                last_exc = exc
                if isinstance(exc, TimeoutError):
                    # 连接/读写超时是对端不可达的日常表现：明确标记为超时，
                    # 保持简洁 warning、不打堆栈（决策 2）。
                    logger.warning(
                        "ADS: connect attempt %d/%d timed out: %s", attempt + 1, budget, exc
                    )
                else:
                    logger.warning(
                        "ADS: connect attempt %d/%d failed: %s", attempt + 1, budget, exc
                    )
                if attempt < budget - 1:
                    await asyncio.sleep(backoff)
                    backoff = min(
                        backoff * _RECONNECT_BACKOFF_MULTIPLIER,
                        self._config.reconnect_backoff_max,
                    )
        self._failed = True
        return last_exc

    async def _do_connect(self) -> None:
        """Create and open the pyads connection (blocking calls on a thread)."""
        pyads = _pyads()
        # ``target_net_id`` defaults to ``ams_net_id``; ``None`` lets pyads
        # auto-detect the Net ID from the IP address, as its ``open()`` does.
        net_id = self._config.target_net_id or None
        connection = pyads.Connection(net_id, self._config.target_port, self._host)
        connection.set_timeout(int(self._config.timeout * 1000))
        await asyncio.to_thread(connection.open)
        if not connection.is_open:
            connection.close()
            raise ProtocolError(f"ADS: failed to open connection to {self._host}")
        self._connection = connection

    def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()

    async def _monitor_loop(self) -> None:
        """Reconnect in the background after a transport failure is signalled."""
        while not self._shutdown:
            await self._reconnect_event.wait()
            self._reconnect_event.clear()
            if self._shutdown:
                return
            if self._connected:
                continue
            last_exc = await self._connect_with_retry()
            if last_exc is not None:
                logger.error("ADS: reconnection retries exhausted: %s", last_exc)
                return

    def _signal_disconnect(self) -> None:
        """Mark the connection as dropped and request a background reconnect."""
        self._connected = False
        self._reconnect_event.set()

    # ------------------------------------------------------------------
    # ProtocolPort — read
    # ------------------------------------------------------------------

    async def read(self, points: list[PointRef]) -> list[PointValue]:
        """Read points using the configured batch strategy (Sum vs sequential)."""
        async with self._lock:
            if not self._connected:
                raise ProtocolError("ADS: cannot read — driver is not connected")
            try:
                if self._config.read_mode == "sum":
                    return await self._read_sum(points)
                return await self._read_sequential(points)
            except ProtocolError:
                raise
            except NotImplementedError:
                # Sum mode with index/offset addressing is a config mismatch, not
                # a wire failure — propagate it unchanged (no reconnect).
                raise
            except Exception as exc:
                # ADSError / transport failures must not leak the third-party
                # type through the port boundary; wrap and schedule a reconnect.
                self._signal_disconnect()
                raise ProtocolError(f"ADS read failed: {exc}") from exc

    async def _read_sum(self, points: list[PointRef]) -> list[PointValue]:
        """Read points via one (or more) ADS Sum commands using symbols.

        Points without a symbol (``index_group``/``index_offset`` addressing)
        are unsupported by the Sum command and raise ``NotImplementedError``.  A
        symbol whose sub-command fails (missing from the response) is reported
        as ``BAD``; an overall Sum failure raises :class:`ProtocolError`.
        """
        results: list[PointValue | None] = [None] * len(points)
        symbol_points: list[tuple[int, PointRef, ADSPoint]] = []
        for i, ref in enumerate(points):
            ap = self._points.get(ref.point_id)
            if ap is None:
                results[i] = self._bad_value(ref)
                continue
            if ap.symbol is None:
                raise NotImplementedError(
                    f"ADS Sum read requires symbol addressing; "
                    f"point '{ref.point_id}' has no symbol"
                )
            symbol_points.append((i, ref, ap))

        max_subs = self._config.max_subs_per_sum
        for start in range(0, len(symbol_points), max_subs):
            chunk = symbol_points[start : start + max_subs]
            symbols = [ap.symbol for _, _, ap in chunk]
            try:
                values = await asyncio.to_thread(self._connection.read_list_by_name, symbols)
            except Exception as exc:
                raise ProtocolError(f"ADS Sum read failed: {exc}") from exc
            for i, ref, ap in chunk:
                if ap.symbol not in values:
                    results[i] = self._bad_value(ref)
                else:
                    results[i] = PointValue(
                        device_id=ref.device_id,
                        point_id=ref.point_id,
                        value=values[ap.symbol],
                        quality=Quality.GOOD,
                        source="ads",
                    )
        return [r for r in results if r is not None]

    async def _read_sequential(self, points: list[PointRef]) -> list[PointValue]:
        """Read each point with an individual ``Read``, concurrency-limited."""
        sem = asyncio.Semaphore(self._config.max_concurrent_reads)

        async def read_one(ref: PointRef) -> PointValue:
            ap = self._points.get(ref.point_id)
            if ap is None:
                return self._bad_value(ref)
            plctype = _plc_datatype(ap.data_type)
            async with sem:
                value = await asyncio.to_thread(
                    self._connection.read, ap.index_group, ap.index_offset, plctype
                )
            return PointValue(
                device_id=ref.device_id,
                point_id=ref.point_id,
                value=value,
                quality=Quality.GOOD,
                source="ads",
            )

        return await asyncio.gather(*(read_one(ref) for ref in points))

    @staticmethod
    def _bad_value(ref: PointRef) -> PointValue:
        """A ``BAD`` value for an unknown or failed point."""
        return PointValue(
            device_id=ref.device_id,
            point_id=ref.point_id,
            value=None,
            quality=Quality.BAD,
            source="ads",
        )

    # ------------------------------------------------------------------
    # ProtocolPort — write
    # ------------------------------------------------------------------

    async def write(self, cmds: list[Command]) -> list[CommandResult]:
        """Batch-write commands; one :class:`CommandResult` per command."""
        async with self._lock:
            if not cmds:
                return []
            if not self._connected:
                raise ProtocolError("ADS: cannot write — driver is not connected")
            try:
                return await self._write_impl(cmds)
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"ADS write failed: {exc}") from exc

    async def _write_impl(self, cmds: list[Command]) -> list[CommandResult]:
        results: list[CommandResult] = []
        for cmd in cmds:
            ap = self._points.get(cmd.point_id)
            if ap is None:
                results.append(self._failed_result(cmd, f"unknown point '{cmd.point_id}'"))
                continue
            plctype = _plc_datatype(ap.data_type)
            await asyncio.to_thread(
                self._connection.write,
                ap.index_group,
                ap.index_offset,
                cmd.value,
                plctype,
            )
            results.append(CommandResult(command_id=cmd.command_id, success=True))
        return results

    @staticmethod
    def _failed_result(cmd: Command, error: str) -> CommandResult:
        return CommandResult(command_id=cmd.command_id, success=False, error=error)

    # ------------------------------------------------------------------
    # ProtocolPort — subscribe / health
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        points: list[PointRef],
        callback: Callable[[PointValue], Awaitable[None]],
    ) -> None:
        """Subscribe to spontaneous device-notification updates.

        Requires ``subscribe.enabled`` in the device config; otherwise raises
        :class:`NotImplementedError`.  Unknown points (not in the point table)
        are skipped.  Calls may be repeated to add points; the subscription
        registers the set difference against its current handles.
        """
        if not self._config.subscribe_enabled:
            raise NotImplementedError(
                "ADS subscription is not enabled — set subscribe.enabled=true "
                "in the device config"
            )
        if self._subscription is None:
            self._subscription = ADSSubscription(
                config=self._config,
                device_id=self._cfg.device_id,
                host=self._host,
                loop=asyncio.get_running_loop(),
                on_data=callback,
            )
        ads_points = [self._points[ref.point_id] for ref in points if ref.point_id in self._points]
        await self._subscription.subscribe(ads_points)

    def health(self) -> HealthStatus:
        """Return cached connection health."""
        if self._failed:
            return HealthStatus(healthy=False, message="FAILED: reconnection retries exhausted")
        if not self._connected:
            return HealthStatus(healthy=False, message="not connected")
        return HealthStatus(healthy=True)


# ---------------------------------------------------------------------------
# self-registration
# ---------------------------------------------------------------------------

from wind_hub.infra.registry import register_protocol  # noqa: E402


@register_protocol("ads")
def _create_ads(cfg: DeviceConfig) -> ProtocolPort:
    return ADSDriver(cfg)
