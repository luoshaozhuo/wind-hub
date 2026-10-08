"""ADS device-notification 订阅句柄。

pyads notification callback 运行在线程中。本模块只在线程回调里构造 ProtocolSample
并通过 event loop 的线程安全入口投递；所有协程和生命周期状态都留在 asyncio
线程。每次重建 notification connection 都重新解析 symbol 地址，避免 PLC
重启/下载后继续使用旧 index_group/index_offset。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from core.application.errors import ConfigError, ProtocolError
from core.application.protocol_contract import ProtocolSample, Quality

from .config import ADSConfig
from .mapping import ADSPoint

logger = logging.getLogger(__name__)
_HEALTH_CHECK_INTERVAL = 1.0


def _pyads() -> Any:
    try:
        import pyads  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ProtocolError("ADS support requires the optional 'pyads' dependency") from exc
    return pyads


def _plc_datatype(ads_name: str) -> Any:
    datatype = getattr(_pyads(), f"PLCTYPE_{ads_name}", None)
    if datatype is None:
        raise ConfigError(f"pyads does not expose PLCTYPE_{ads_name}")
    return datatype


class ADSSubscription:
    """一次独立 ADS notification 订阅。

    句柄拥有自己的 notification connection pool，因此不会与主 read/write
    connection 争用同步 pyads session。close 返回前会注销 notification、
    关闭连接并等待已开始的 callback 执行完毕。
    """

    def __init__(
        self,
        config: ADSConfig,
        points: Sequence[ADSPoint],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        cycle_time: float,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        if cycle_time <= 0:
            raise ConfigError("ADS subscription interval must be > 0")

        self._config = config
        self._points = tuple(points)
        self._callback = callback
        self._cycle_time = cycle_time
        self._loop = loop

        self._connections: list[Any] = []
        self._handles: list[tuple[Any, Any, Any]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._monitor_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """建立 notification pool 并注册全部点。"""
        async with self._lock:
            if self._closed:
                raise ProtocolError("ADS subscription is closed")
            await self._rebuild_locked()
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name="ads-notification-monitor",
            )

    async def close(self) -> None:
        """停止订阅并等待在途 callback 完成；重复调用安全。"""
        if self._closed:
            return
        self._closed = True

        monitor, self._monitor_task = self._monitor_task, None
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor

        async with self._lock:
            await self._close_pool_locked()

        current = asyncio.current_task()
        tasks = tuple(task for task in self._tasks if task is not current)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _monitor_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL)
            if self._closed:
                return
            try:
                async with self._lock:
                    if self._points and len(self._handles) != len(self._points):
                        raise ConnectionError("ADS notification registration is incomplete")
                    for connection in self._connections:
                        if not connection.is_open:
                            raise ConnectionError("ADS notification connection is closed")
                        await asyncio.to_thread(connection.read_state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "ADS notification connection lost: %s; rebuilding",
                    exc,
                )
                try:
                    async with self._lock:
                        if not self._closed:
                            await self._rebuild_locked()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("ADS notification pool rebuild failed")

    async def _rebuild_locked(self) -> None:
        await self._close_pool_locked()
        if not self._points:
            return

        max_per = self._config.max_notifications_per_connection
        for start in range(0, len(self._points), max_per):
            chunk = self._points[start : start + max_per]
            connection = await self._create_connection()
            self._connections.append(connection)
            try:
                for point in chunk:
                    resolved = await self._resolve_point(
                        connection,
                        point,
                    )
                    await self._register_point(
                        connection,
                        resolved,
                    )
            except Exception:
                await self._close_pool_locked()
                raise

    async def _create_connection(self) -> Any:
        pyads = _pyads()
        connection = pyads.Connection(
            self._config.target_net_id or None,
            self._config.target_port,
            self._config.host,
        )
        connection.set_timeout(int(self._config.timeout * 1000))
        try:
            await asyncio.to_thread(connection.open)
            if not connection.is_open:
                raise ConnectionError(
                    f"ADS notification connection to "
                    f"{self._config.host}:{self._config.target_port} did not open"
                )
            await asyncio.to_thread(connection.read_state)
        except Exception:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.close)
            raise
        return connection

    async def _resolve_point(
        self,
        connection: Any,
        point: ADSPoint,
    ) -> ADSPoint:
        if point.symbol is None:
            if not point.address_resolved:
                raise ConfigError(f"ADS point '{point.point_id}' has no resolved address")
            return point

        symbol = await asyncio.to_thread(
            connection.get_symbol,
            point.symbol,
        )
        index_group = getattr(symbol, "index_group", None)
        index_offset = getattr(symbol, "index_offset", None)
        if (
            isinstance(index_group, bool)
            or not isinstance(index_group, int)
            or isinstance(index_offset, bool)
            or not isinstance(index_offset, int)
        ):
            raise ConfigError(f"ADS symbol '{point.symbol}' did not resolve a valid index address")

        size = point.size
        plc_type = getattr(symbol, "plc_type", None)
        if plc_type is not None:
            with contextlib.suppress(TypeError):
                size = ctypes.sizeof(plc_type)

        return point.resolved(
            index_group=index_group,
            index_offset=index_offset,
            size=size,
        )

    async def _register_point(
        self,
        connection: Any,
        point: ADSPoint,
    ) -> None:
        if point.size <= 0:
            raise ConfigError(
                f"ADS notification point '{point.point_id}' requires a "
                "resolved positive byte size"
            )

        pyads = _pyads()
        attr = pyads.NotificationAttrib(
            point.size,
            cycle_time=self._cycle_time,
            max_delay=self._config.max_delay,
        )
        callback = connection.notification(_plc_datatype(point.data_type))(
            self._notification_callback(point)
        )
        handle, user_handle = await asyncio.to_thread(
            connection.add_device_notification,
            (point.index_group, point.index_offset),
            attr,
            callback,
        )
        self._handles.append((connection, handle, user_handle))

    async def _close_pool_locked(self) -> None:
        for connection, handle, user_handle in self._handles:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    connection.del_device_notification,
                    handle,
                    user_handle,
                )
        self._handles.clear()

        for connection in self._connections:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.close)
        self._connections.clear()

    def _notification_callback(
        self,
        point: ADSPoint,
    ) -> Callable[..., None]:
        def callback(
            handle: Any,
            address: Any,
            timestamp: Any,
            value: Any,
        ) -> None:
            del handle, address
            if self._closed:
                return
            try:
                scalar = _as_scalar(value)
                quality = Quality.GOOD
            except TypeError:
                scalar = None
                quality = Quality.BAD

            sample = ProtocolSample(
                point_id=point.point_id,
                value=scalar,
                quality=quality,
                timestamp=_normalize_timestamp(timestamp),
            )
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(
                    self._schedule_sample,
                    sample,
                )

        return callback

    def _schedule_sample(self, sample: ProtocolSample) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._invoke_callback(sample))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _invoke_callback(self, sample: ProtocolSample) -> None:
        try:
            await self._callback(sample)
        except Exception:
            logger.exception(
                "ADS subscriber callback failed for point '%s'",
                sample.point_id,
            )


def _normalize_timestamp(value: object) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime):
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_scalar(value: object) -> float | int | bool | str | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported ADS notification value type '{type(value).__name__}'")
