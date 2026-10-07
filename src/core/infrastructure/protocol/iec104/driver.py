"""基于 c104/lib60870-C 的共享 IEC104 ProtocolPort Adapter。

Shared Core 只提供单连接 connect/read/write/health 和显式总召能力。持续采集、
订阅分发、Task 生命周期和重连策略由上层应用负责。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from core.application import (
    ConfigError,
    ConnectionHealth,
    DeviceConnection,
    ProtocolError,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    SubscriptionHandle,
)
from core.domain import PointAccess, PointTable, ProtocolPoint

from .codec import command_to_c104, sample_from_c104
from .config import IEC104Config, parse_iec104_config
from .mapping import (
    IEC104Point,
    build_iec104_index,
    validate_iec104_write_type,
)



class _Subscription:
    """一次 IEC104 样本订阅，close 带 in-flight drain barrier。"""

    def __init__(
        self,
        registry: "_SubscriptionRegistry",
        callback: Callable[[ProtocolSample], Awaitable[None]],
        ioas: tuple[int, ...] | None,
    ) -> None:
        self._registry = registry
        self._callback = callback
        self._ioas = ioas
        self._closed = False
        self._in_flight = 0
        self._idle: asyncio.Event | None = None

    async def close(self) -> None:
        """注销订阅并等待已开始 callback 完成。"""
        if self._closed:
            return
        self._closed = True
        self._registry.unsubscribe(self)
        while self._in_flight:
            self._idle = asyncio.Event()
            await self._idle.wait()
            self._idle = None

    def _track(self) -> bool:
        if self._closed:
            return False
        self._in_flight += 1
        return True

    def _untrack(self) -> None:
        self._in_flight -= 1
        if self._in_flight == 0 and self._idle is not None:
            self._idle.set()


class _SubscriptionRegistry:
    """仅由 IEC104Driver 所属 asyncio loop 访问的订阅表。"""

    def __init__(self) -> None:
        self._global: list[_Subscription] = []
        self._ioa: dict[int, list[_Subscription]] = {}

    def subscribe(
        self,
        ioas: tuple[int, ...] | None,
        callback: Callable[[ProtocolSample], Awaitable[None]],
    ) -> _Subscription:
        subscription = _Subscription(self, callback, ioas)
        if ioas is None:
            self._global.append(subscription)
            return subscription
        for ioa in ioas:
            self._ioa.setdefault(ioa, []).append(subscription)
        return subscription

    def unsubscribe(self, subscription: _Subscription) -> None:
        if subscription._ioas is None:
            with contextlib.suppress(ValueError):
                self._global.remove(subscription)
            return
        for ioa in subscription._ioas:
            subscriptions = self._ioa.get(ioa)
            if subscriptions is None:
                continue
            with contextlib.suppress(ValueError):
                subscriptions.remove(subscription)
            if not subscriptions:
                del self._ioa[ioa]

    def clear(self) -> None:
        for subscription in self._global:
            subscription._closed = True
        for subscriptions in self._ioa.values():
            for subscription in subscriptions:
                subscription._closed = True
        self._global.clear()
        self._ioa.clear()

    async def dispatch(self, sample: ProtocolSample, ioa: int) -> None:
        subscriptions = [
            subscription
            for subscription in [
                *self._global,
                *self._ioa.get(ioa, []),
            ]
            if subscription._track()
        ]
        for subscription in subscriptions:
            asyncio.create_task(
                self._invoke(subscription, sample)
            )

    async def _invoke(
        self,
        subscription: _Subscription,
        sample: ProtocolSample,
    ) -> None:
        try:
            await subscription._callback(sample)
        finally:
            subscription._untrack()


def _c104() -> Any:
    try:
        import c104
    except ImportError as exc:
        raise ConfigError(
            "IEC104 support requires the optional 'c104' dependency"
        ) from exc
    return c104


class IEC104Driver:
    """单个 DeviceConnection 的 IEC104 主站 Driver。

    c104 自己运行 APCI/ASDU 状态机和内部重连。本 Driver 只维护共享镜像，
    断线时立即清空镜像，避免 read 返回旧值。
    """

    def __init__(
        self,
        connection: DeviceConnection,
        point_table: PointTable,
    ) -> None:
        if point_table.protocol.name != "iec104":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'iec104'"
            )

        self._connection_config = connection
        self._config: IEC104Config = parse_iec104_config(connection)
        by_id, by_ioa = build_iec104_index(list(point_table.points.values()))
        self._points_by_id = by_id
        self._points_by_ioa = by_ioa

        for point in point_table.points.values():
            mapped = self._points_by_id[point.point_id]
            if point.access in (PointAccess.WRITE, PointAccess.READ_WRITE):
                validate_iec104_write_type(mapped)

        self._lock = asyncio.Lock()
        self._client: Any = None
        self._connection: Any = None
        self._station: Any = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._open_event: asyncio.Event | None = None
        self._is_open = False
        self._closed = True

        self._samples: dict[int, ProtocolSample] = {}
        self._command_locks: dict[int, asyncio.Lock] = {}
        self._receive_callback_factory: Any = None
        self._subscriptions = _SubscriptionRegistry()

    async def connect(self) -> None:
        """创建 c104 client 并等待连接进入 OPEN。"""
        async with self._lock:
            if self._client is not None and self._is_open:
                return

            c104 = _c104()
            self._closed = False
            self._is_open = False
            self._loop = asyncio.get_running_loop()
            self._open_event = asyncio.Event()
            self._samples.clear()

            client = c104.Client(
                command_timeout_ms=int(self._config.t1 * 2 * 1000)
            )
            connection = client.add_connection(
                ip=self._config.host,
                port=self._config.port,
                init=c104.Init.NONE,
            )
            if connection is None:
                self._closed = True
                raise ProtocolError(
                    f"IEC104 invalid endpoint "
                    f"{self._config.host}:{self._config.port}"
                )

            self._apply_protocol_parameters(connection)
            station = connection.add_station(
                common_address=self._config.common_addr
            )
            if station is None:
                self._closed = True
                raise ProtocolError(
                    f"IEC104 invalid common_addr={self._config.common_addr}"
                )

            from .callbacks import (
                new_point_callback,
                receive_callback,
                state_callback,
            )

            connection.on_state_change(
                callable=state_callback(self._handle_state_change)
            )
            client.on_new_point(
                callable=new_point_callback(self._handle_new_point)
            )
            self._receive_callback_factory = receive_callback

            self._client = client
            self._connection = connection
            self._station = station

            try:
                await asyncio.to_thread(client.start)
                connection.connect()
                await asyncio.wait_for(
                    self._open_event.wait(),
                    timeout=self._config.t0,
                )
            except TimeoutError:
                await self._cleanup_failed_connect()
                raise ProtocolError(
                    f"IEC104 connect to {self._config.host}:"
                    f"{self._config.port} timed out after {self._config.t0}s"
                ) from None
            except Exception as exc:
                await self._cleanup_failed_connect()
                raise ProtocolError(
                    f"IEC104 connect failed for {self._config.host}:"
                    f"{self._config.port}: {exc}"
                ) from exc

            self._is_open = True

    async def close(self) -> None:
        """关闭连接与 c104 client；重复调用安全。"""
        async with self._lock:
            if self._closed and self._client is None:
                return

            self._closed = True
            self._is_open = False
            client, connection = self._client, self._connection
            self._client = None
            self._connection = None
            self._station = None
            self._open_event = None
            self._receive_callback_factory = None
            self._samples.clear()
            self._subscriptions.clear()
            self._loop = None

            if connection is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(connection.disconnect)
            if client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(client.stop)

    def health(self) -> ConnectionHealth:
        """返回缓存的 OPEN 状态，不触发网络 I/O。"""
        if self._is_open:
            return ConnectionHealth(
                healthy=True,
                message=f"connected to {self._config.host}:{self._config.port}",
            )
        if self._client is None:
            return ConnectionHealth(healthy=False, message="not connected")
        return ConnectionHealth(
            healthy=False,
            message="IEC104 connection is not OPEN",
        )

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        """读取 c104 回调维护的最新镜像，不主动发 wire 请求。"""
        if not points:
            return ()
        if not self._is_open:
            raise ProtocolError("IEC104 read requires an OPEN connection")

        results: list[ProtocolSample] = []
        for point in points:
            mapped = self._mapped_point(point)
            sample = self._samples.get(mapped.ioa)
            if sample is None:
                results.append(
                    ProtocolSample(
                        point_id=point.point_id,
                        value=None,
                        quality=Quality.BAD,
                    )
                )
            else:
                results.append(sample)
        return tuple(results)

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """执行 IEC104 遥控/设点，并按 ACT_CON 结果返回逐点状态。"""
        if not writes:
            return ()
        if not self._is_open or self._station is None:
            raise ProtocolError("IEC104 write requires an OPEN connection")

        return tuple(
            await asyncio.gather(
                *(self._execute_write(write) for write in writes)
            )
        )

    async def subscribe(
        self,
        points: Sequence[ProtocolPoint],
        callback: Callable[[ProtocolSample], Awaitable[None]],
    ) -> SubscriptionHandle:
        """注册 IEC104 主动上送/总召响应的样本回调。

        空 points 表示订阅本 PointTable 内的全部已知 IOA。生命周期由调用方
        持有返回句柄；订阅不自动触发总召。
        """
        if points:
            ioas = tuple(self._mapped_point(point).ioa for point in points)
        else:
            ioas = None
        return self._subscriptions.subscribe(ioas, callback)

    async def interrogate(self) -> None:
        """显式发送一次 General Interrogation（QOI=20）。"""
        if not self._is_open or self._connection is None:
            raise ProtocolError(
                "IEC104 interrogation requires an OPEN connection"
            )

        c104 = _c104()
        connection = self._connection
        accepted = await asyncio.to_thread(
            connection.interrogation,
            common_address=self._config.common_addr,
            cause=c104.Cot.ACTIVATION,
            qualifier=c104.Qoi.STATION,
        )
        if not accepted:
            raise ProtocolError(
                f"IEC104 general interrogation rejected by "
                f"{self._config.host}:{self._config.port}"
            )

    async def _execute_write(
        self,
        write: ProtocolWrite,
    ) -> ProtocolWriteResult:
        mapped = self._mapped_point(write.point)
        lock = self._command_locks.setdefault(
            mapped.ioa,
            asyncio.Lock(),
        )

        async with lock:
            try:
                point_type, value = command_to_c104(
                    mapped,
                    write.value,
                )
                point = self._get_or_create_command_point(
                    mapped.ioa,
                    point_type,
                )
            except (ConfigError, ProtocolError, TypeError, ValueError) as exc:
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    success=False,
                    message=str(exc),
                )

            point.value = value
            c104 = _c104()
            started = time.monotonic()
            try:
                accepted = await asyncio.to_thread(
                    point.transmit,
                    c104.Cot.ACTIVATION,
                )
            except Exception as exc:
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    success=False,
                    message=str(exc),
                )
            elapsed = time.monotonic() - started

        if accepted:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                success=True,
            )
        return ProtocolWriteResult(
            point_id=mapped.point_id,
            success=False,
            message=self._command_failure_reason(elapsed),
        )

    def _get_or_create_command_point(
        self,
        ioa: int,
        point_type: Any,
    ) -> Any:
        station = self._station
        if station is None:
            raise ProtocolError("IEC104 station is not available")

        existing = station.get_point(ioa)
        if existing is not None:
            if existing.type == point_type:
                return existing
            raise ProtocolError(
                f"IEC104 IOA {ioa} already registered as "
                f"{existing.type.name}, cannot send {point_type.name}"
            )

        point = station.add_point(
            io_address=ioa,
            type=point_type,
        )
        if point is None:
            raise ProtocolError(
                f"IEC104 cannot create command point at IOA {ioa}"
            )
        return point

    def _mapped_point(self, point: ProtocolPoint) -> IEC104Point:
        mapped = self._points_by_id.get(point.point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point.point_id}' is not part of connection "
                f"'{self._connection_config.connection_id}' point table"
            )
        return mapped

    def _apply_protocol_parameters(self, connection: Any) -> None:
        params = connection.protocol_parameters
        cfg = self._config
        params.connection_timeout = int(cfg.t0)
        params.message_timeout = int(cfg.t1)
        params.confirm_interval = int(cfg.t2)
        params.keep_alive_interval = int(cfg.t3)
        params.send_window_size = cfg.k
        params.receive_window_size = cfg.w

    async def _cleanup_failed_connect(self) -> None:
        self._closed = True
        self._is_open = False
        client, connection = self._client, self._connection
        self._client = None
        self._connection = None
        self._station = None
        self._open_event = None
        self._receive_callback_factory = None
        self._samples.clear()
        self._loop = None

        if connection is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.disconnect)
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.stop)

    def _command_failure_reason(self, elapsed: float) -> str:
        if not self._is_open:
            return "connection lost"
        if elapsed >= self._config.t1 * 2 * 0.9:
            return "timeout"
        return "negative confirmation"

    def _handle_state_change(self, state: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(
                self._handle_state,
                state,
            )

    def _handle_state(self, state: Any) -> None:
        if self._closed:
            return
        c104 = _c104()
        if state == c104.ConnectionState.OPEN:
            self._is_open = True
            if self._open_event is not None:
                self._open_event.set()
            return

        self._is_open = False
        self._samples.clear()

    def _handle_new_point(
        self,
        station: Any,
        io_address: int,
        point_type: Any,
    ) -> None:
        if self._closed:
            return
        if io_address not in self._points_by_ioa:
            return

        factory = self._receive_callback_factory
        if factory is None:
            return

        point = station.get_point(io_address)
        if point is None:
            point = station.add_point(
                io_address=io_address,
                type=point_type,
            )
        if point is None:
            return
        point.on_receive(
            callable=factory(self._handle_point_receive)
        )

    def _handle_point_receive(self, point: Any) -> Any:
        c104 = _c104()
        ioa = point.io_address
        mapped = self._points_by_ioa.get(ioa)
        if mapped is None:
            return c104.ResponseState.NONE

        try:
            sample = sample_from_c104(
                point,
                mapped.point_id,
            )
        except (TypeError, ValueError):
            sample = ProtocolSample(
                point_id=mapped.point_id,
                value=None,
                quality=Quality.BAD,
            )

        loop = self._loop
        if loop is not None and not self._closed:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    self._store_sample,
                    ioa,
                    sample,
                )
        return c104.ResponseState.NONE

    def _store_sample(
        self,
        ioa: int,
        sample: ProtocolSample,
    ) -> None:
        if self._closed or not self._is_open:
            return
        self._samples[ioa] = sample
        asyncio.create_task(
            self._subscriptions.dispatch(sample, ioa)
        )
