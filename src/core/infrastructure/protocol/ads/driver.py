"""基于 pyads 的共享 ADS ProtocolPort Adapter。

Driver 实现共享的 connect/read/write/health，并通过可选订阅能力暴露 ADS
device-notification。何时订阅、订阅哪些点以及失败后的应用级重建策略仍由上层决定。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import struct
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pyads  # type: ignore[import-untyped]

from core.application.errors import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WritableScalar,
    WriteOutcome,
)
from core.application.settings import ADSLocalConfig
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .config import ADSConfig, parse_ads_config
from .mapping import ADSPoint, parse_ads_point, symbol_index_address
from .subscription import ADSSubscription

_ADSERR_SYMBOL_NOT_FOUND = 1808

logger = logging.getLogger(__name__)
_REMOTE_ROUTE_LOCK = threading.Lock()
_LOCAL_AMS_LOCK = threading.RLock()
_LOCAL_AMS: ADSLocalConfig | None = None
_LOCAL_PORT_OPEN = False


def _ads_state_name(value: object) -> str:
    """把 pyads ADSSTATE_* 数值转成可诊断名称；未知值保留原值。"""
    constants = getattr(pyads, "constants", None)
    if constants is not None:
        for name in dir(constants):
            if name.startswith("ADSSTATE_") and getattr(constants, name, object()) == value:
                return name.removeprefix("ADSSTATE_")
    return str(value)


def _plc_datatype(ads_name: str) -> Any:
    datatype = getattr(pyads, f"PLCTYPE_{ads_name}", None)
    if datatype is None:
        raise ConfigError(f"pyads does not expose PLCTYPE_{ads_name}")
    return datatype


class ADSDriver:
    """单个 Endpoint 的 ADS Driver。

    symbol 点在每次 ADS session 建立后解析为 index_group/index_offset 并缓存；
    read/write 不在周期路径上重复查询 symbol。
    """

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        if point_table.protocol.name != "ads":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'ads'"
            )

        self._point_table_id = point_table.point_table_id
        self._config: ADSConfig = parse_ads_config(
            endpoint,
            protocol_options,
        )

        self._lock = asyncio.Lock()
        self._connection: Any = None
        self._connected = False
        self._subscriptions: set[ADSSubscription] = set()
        # 断连清理 task 的强引用集合——fire-and-forget task 只有弱引用会被
        # event loop 提前 GC（"Task was destroyed but it is pending"）；
        # done callback 同时取回异常，避免 "exception was never retrieved"。
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        # 已解析动态点与注册点分别复用；仅当前 ADS session 有效。
        self._symbol_cache: dict[str, tuple[int, int, int | None]] = {}
        # ReadPlan 按排序后的完整点序列缓存，包含分批地址和解码器。
        self._read_plan_cache: dict[tuple[ADSPoint, ...], tuple[Any, ...]] = {}
        self._points: dict[str, ADSPoint] = {}
        self.update_point_table(point_table)

    def update_point_table(self, point_table: PointTable) -> None:
        """热重载点表：重建符号/类型映射并失效读取缓存（不断开连接）。

        进行中的 ADS 订阅仍按旧 symbol 通知，由上层重启订阅后按新映射
        重新注册。
        """
        # update_point_table 是同步方法，无法在这里异步查询 PLC Symbol。
        # 对于在线 Symbol 表变更，要求通过 reconnect 重新初始化会话地址。
        if self._connected and any(
            point.ext.get("symbol") is not None for point in point_table.points.values()
        ):
            raise ConfigError("ADS symbol point table update requires reconnect")
        self._point_table_id = point_table.point_table_id
        self._points = {
            point.point_id: parse_ads_point(point) for point in point_table.points.values()
        }
        self._read_plan_cache.clear()
        self._symbol_cache.clear()

    def capabilities(self) -> frozenset[ProtocolCapability]:
        """返回 ADS Driver 实际支持的协议能力。"""
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.WRITE_MANY,
                ProtocolCapability.SUBSCRIBE,
            }
        )

    @staticmethod
    def _initialize_local_sync(config: ADSLocalConfig) -> None:
        """Initialize process-wide AMS identity once, independently of device sessions."""
        global _LOCAL_AMS, _LOCAL_PORT_OPEN
        with _LOCAL_AMS_LOCK:
            if _LOCAL_AMS is not None:
                if (
                    _LOCAL_AMS.local_ams_net_id != config.local_ams_net_id
                    or _LOCAL_AMS.local_ip != config.local_ip
                ):
                    raise ConfigError("ADS local AMS identity conflicts with initialized identity")
                _LOCAL_AMS = config
                return
            if sys.platform.startswith("linux"):
                pyads.open_port()
                try:
                    pyads.set_local_address(config.local_ams_net_id)
                except BaseException:
                    pyads.close_port()
                    raise
                _LOCAL_PORT_OPEN = True
            _LOCAL_AMS = config

    @classmethod
    async def initialize_local(cls, config: ADSLocalConfig) -> None:
        await asyncio.to_thread(cls._initialize_local_sync, config)

    @classmethod
    async def close_local(cls) -> None:
        """释放进程级本地 AMS 端口；不得在其他 ADS Driver 工作期间调用。"""
        global _LOCAL_AMS, _LOCAL_PORT_OPEN
        with _LOCAL_AMS_LOCK:
            if _LOCAL_PORT_OPEN:
                pyads.close_port()
            _LOCAL_PORT_OPEN = False
            _LOCAL_AMS = None

    def _new_connection(self) -> Any:
        """创建配置一致、尚未 open 的 pyads Connection。"""
        connection = pyads.Connection(
            self._config.target_net_id or None,
            self._config.target_port,
            self._config.host,
        )
        return connection

    async def connect(self) -> None:
        """连接并检查 PLC RUN 状态；失败时可添加一次反向路由再重连。"""
        async with self._lock:
            if self._connected and self._connection is not None:
                return
            self._close_connection()
            last_health: ConnectionHealth | None = None

            for attempt in range(2 if self._config.route_auto_add else 1):
                try:
                    connection = self._new_connection()
                    self._connection = connection
                    await asyncio.to_thread(connection.open)
                    if not connection.is_open:
                        raise ConnectionError("ADS session did not open")
                    await asyncio.to_thread(
                        connection.set_timeout, int(self._config.timeout * 1000)
                    )
                    last_health = await self.health()
                    if last_health.healthy:
                        self._invalidate_symbol_addresses()
                        await self._resolve_configured_points()
                        self._connected = True
                        return
                except Exception:
                    last_health = ConnectionHealth(
                        healthy=False, message=f"ADS connection failed for {self._config.host}"
                    )
                finally:
                    if not self._connected:
                        connection = self._connection
                        self._connection = None
                        if connection is not None:
                            with contextlib.suppress(Exception):
                                await asyncio.to_thread(connection.close)

                if attempt == 0 and self._config.route_auto_add:
                    identity = _LOCAL_AMS
                    if identity is None:
                        raise ConfigError("ADS local AMS identity is not initialized")
                    try:
                        # 路由是 PLC 持久资源；pyads.delete_route() 只删本地路由。
                        def add_route() -> bool:
                            with _REMOTE_ROUTE_LOCK:
                                return bool(
                                    pyads.add_route_to_plc(
                                        identity.local_ams_net_id,
                                        identity.local_ip,
                                        self._config.host,
                                        self._config.username,
                                        self._config.password,
                                    )
                                )

                        added = await asyncio.to_thread(add_route)
                        if not added:
                            raise RuntimeError("remote route registration refused")
                    except Exception:
                        raise ProtocolError(
                            f"ADS remote route registration failed for {self._config.host}"
                        ) from None

            raise ProtocolError(
                f"ADS connect failed for {self._config.host}: "
                f"{last_health.message if last_health else 'unknown failure'}"
            )

    async def close(self) -> None:
        """关闭当前 ADS session；重复调用安全。"""
        async with self._lock:
            connection, subscriptions = self._detach_runtime()

        await self._close_detached(connection, subscriptions)

    def is_open(self) -> bool:
        """本地 ADS session 是否处于已连接状态（含路由与符号解析完成）。"""
        return self._connected

    async def health(self) -> ConnectionHealth:
        """在当前业务连接上读取 ADS 状态；仅 PLC ADS State=RUN 为健康。"""
        connection = self._connection
        if connection is None or not connection.is_open:
            return ConnectionHealth(healthy=False, message="ADS session is not open")
        try:
            state = await asyncio.to_thread(connection.read_state)
            if state is None:
                return ConnectionHealth(healthy=False, message="ADS state read returned None")
            ads_state, device_state = state
            run_state = getattr(getattr(pyads, "constants", None), "ADSSTATE_RUN", 5)
            return ConnectionHealth(
                healthy=ads_state == run_state,
                message=(
                    f"ads_state={_ads_state_name(ads_state)}, "
                    f"device_state={device_state}"
                ),
            )
        except Exception as exc:
            return ConnectionHealth(healthy=False, message=f"ADS state read failed: {exc}")

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> _TrackedADSSubscription:
        """建立独立 ADS device-notification 订阅。

        interval 是 notification cycle_time，必须由调用方明确提供。空 point_ids
        表示订阅当前 PointTable 的全部协议点。
        """
        if not self._connected:
            raise ProtocolError("ADS subscribe requires an active connection")
        if interval is None or interval <= 0:
            raise ConfigError("ADS subscription interval must be > 0")

        mapped = (
            tuple(self._mapped_point(point_id) for point_id in point_ids)
            if point_ids
            else tuple(self._points.values())
        )

        subscription = ADSSubscription(
            self._config,
            mapped,
            callback,
            cycle_time=interval,
            loop=asyncio.get_running_loop(),
        )
        await subscription.start()
        self._subscriptions.add(subscription)
        return _TrackedADSSubscription(
            self,
            subscription,
        )

    async def read_one(self, point: str) -> ProtocolSample:
        """读取一个逻辑点；低频路径，不走 Sum Read、分段或读取分组缓存。

        只接受已配置 Point Key，地址在连接建立时预先解析。
        """
        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS read requires an active connection")
            mapped = self._resolve_point(point)
            point_id = mapped.point_id
            try:
                value = await asyncio.to_thread(
                    self._connection.read,
                    mapped.group,
                    mapped.offset,
                    _plc_datatype(mapped.data_type),
                )
            except Exception as exc:
                if _is_point_level_error(exc):
                    return _bad_sample(point_id)
                raise ProtocolError(f"ADS read failed: {exc}") from exc
            try:
                scalar = _as_point_scalar(value)
            except TypeError:
                return _bad_sample(point_id)
            return ProtocolSample(
                point_id=point_id,
                value=scalar,
                timestamp=datetime.now(UTC),
                quality=Quality.GOOD,
            )

    async def write_one(
        self,
        write: ProtocolWrite,
    ) -> ProtocolWriteResult:
        """写入一个逻辑点；低频路径，不使用 Sum Write 批量规划。

        ProtocolWrite.point 必须是已配置的 Point Key。

        配置/取值级问题返回失败结果；连接/传输级问题统一抛 ProtocolError。
        """
        mapped = self._resolve_point(write.point)
        write_value = write.value
        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS write requires an active connection")
            try:
                return await self._write_single_point(mapped, write_value)
            except Exception as exc:
                raise ProtocolError(f"ADS write failed: {exc}") from exc

    async def read_many(
        self,
        points: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        """使用 ADS Sum Read 批量读取，返回按输入顺序排列的样本元组。

        支持注册点 point_id 与动态 ADSPoint 混合；动态点按显式 index 地址
        参与 Sum Read，不降级为逐点读取。
        """
        raw = await self._read_values(points)
        # 全批次共享同一读取时刻——Driver 在获得读取结果时生成 UTC 时间戳。
        received_at = datetime.now(UTC)
        return tuple(
            ProtocolSample(point_id=pid, value=value, timestamp=received_at, quality=quality)
            for pid, (value, quality) in zip(_point_identities(points), raw, strict=True)
        )

    async def _read_values(
        self,
        points: Sequence[str],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """按已配置的地址直接执行 Sum Read。"""
        if not points:
            return ()
        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS read requires an active connection")
            try:
                mapped = tuple(self._resolve_point(point) for point in points)
                return await self._read_sum_raw(mapped)
            except ProtocolError:
                raise
            except Exception as exc:
                raise ProtocolError(f"ADS read failed: {exc}") from exc

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """按已解析 index 地址批量写入。

        配置级问题收敛为单点失败；连接/传输级问题统一抛 ProtocolError。
        """
        if not writes:
            return ()

        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS write requires an active connection")

            results: list[ProtocolWriteResult | None] = [None] * len(writes)
            mapped_batch = [self._resolve_point(write.point) for write in writes]
            prepared: list[tuple[int, ADSPoint, bytes]] = []
            for index, (write, point) in enumerate(zip(writes, mapped_batch, strict=True)):
                if not point.address_resolved:
                    results[index] = ProtocolWriteResult(
                        point_id=write.point_id,
                        outcome=WriteOutcome.CONFIRMED_FAILURE,
                        message="ADS address is unresolved",
                    )
                    continue
                try:
                    raw_value = _coerce_write_value(write.value, point.data_type)
                    payload = _encode_value(raw_value, point)
                except (ConfigError, TypeError, ValueError, OverflowError) as exc:
                    results[index] = ProtocolWriteResult(
                        point_id=write.point_id,
                        outcome=WriteOutcome.CONFIRMED_FAILURE,
                        message=str(exc),
                    )
                    continue
                prepared.append((index, point, payload))

            try:
                for start in range(0, len(prepared), self._config.max_subs_per_sum):
                    chunk = prepared[start : start + self._config.max_subs_per_sum]
                    errors = await asyncio.to_thread(self._sum_write_bytes, chunk)
                    for (index, point, _), error in zip(chunk, errors, strict=True):
                        results[index] = ProtocolWriteResult(
                            point_id=point.point_id,
                            # error == 0 为设备确认成功；否则设备返回明确
                            # ADS 错误码——两种结果都有协议证据。
                            outcome=(
                                WriteOutcome.CONFIRMED_SUCCESS
                                if error == 0
                                else WriteOutcome.CONFIRMED_FAILURE
                            ),
                            message=None if error == 0 else f"ADS write error {error}",
                        )
            except Exception as exc:
                raise ProtocolError(f"ADS sum write failed: {exc}") from exc

            if any(result is None for result in results):
                raise RuntimeError("ADS sum write did not produce a result for every point")
            return tuple(result for result in results if result is not None)

    def _sum_write_bytes(
        self,
        chunk: Sequence[tuple[int, ADSPoint, bytes]],
    ) -> tuple[int, ...]:
        """Send ADSIGRP_SUMUP_WRITE by index and decode individual ADS error codes."""
        from pyads.constants import ADSIGRP_SUMUP_WRITE  # type: ignore[import-untyped]

        connection = self._connection
        if connection is None:
            raise ProtocolError("ADS connection is unavailable")
        headers = b"".join(
            struct.pack("<III", point.group, point.offset, len(payload))
            for _, point, payload in chunk
        )
        request = bytearray(headers + b"".join(payload for _, _, payload in chunk))
        raw = connection.read_write(
            ADSIGRP_SUMUP_WRITE,
            len(chunk),
            None,
            request,
            None,
            check_length=False,
        )
        if raw is None or len(raw) != len(chunk) * 4:
            raise ProtocolError("ADS sum write returned an invalid response size")
        return tuple(error for (error,) in struct.iter_unpack("<I", bytes(raw)))

    async def _write_single_point(
        self,
        mapped: ADSPoint,
        value: object,
    ) -> ProtocolWriteResult:
        """按已解析 index 地址写入单点；配置/取值级问题收敛为单点失败。

        连接/传输级异常原样抛出，由调用方上报，不擅自关闭会话。
        """
        if not mapped.address_resolved:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message="ADS address is unresolved",
            )

        try:
            datatype = _plc_datatype(mapped.data_type)
        except ConfigError as exc:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message=str(exc),
            )

        try:
            raw_value = _coerce_write_value(
                value,
                mapped.data_type,
            )
        except (TypeError, ValueError) as exc:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message=str(exc),
            )

        try:
            await asyncio.to_thread(
                self._connection.write,
                mapped.group,
                mapped.offset,
                raw_value,
                datatype,
            )
        except Exception as exc:
            # 点级 ADS 错误（如 symbol 在 PLC 侧已删除）只收敛为单点失败；
            # 连接/传输级异常继续抛出，由调用方统一断线处理。
            if _is_point_level_error(exc):
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.CONFIRMED_FAILURE,
                    message=str(exc) or type(exc).__name__,
                )
            raise
        return ProtocolWriteResult(
            point_id=mapped.point_id,
            outcome=WriteOutcome.CONFIRMED_SUCCESS,
        )

    async def interrogate(self) -> None:
        """ADS 不支持 IEC104 式总召能力。"""
        raise ProtocolCapabilityError("ads does not support interrogation")

    async def _read_sum_raw(
        self,
        points: Sequence[ADSPoint],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """单份计划缓存：排序编译、Sum Read、按原输入顺序还原结果。"""
        key = tuple(sorted(points, key=lambda point: (
            point.point_id, point.group if point.address_resolved else -1,
            point.offset if point.address_resolved else -1,
            point.data_type, point.size, point.symbol or "",
        )))
        plan = self._read_plan_cache.get(key)
        if plan is None:
            entries = []
            for point in key:
                if not point.address_resolved or point.size <= 0:
                    entries.append((point, None))
                    continue
                if point.data_type == "STRING":
                    decoder = lambda raw: raw.split(b"\\x00", 1)[0].decode("utf-8")
                else:
                    datatype = _plc_datatype(point.data_type)
                    def decoder(raw: bytes, datatype: Any = datatype) -> PointScalar:
                        value = datatype.from_buffer_copy(raw)
                        return _as_point_scalar(value.value if hasattr(value, "value") else value)
                entries.append((point, decoder))

            plan = tuple(
                tuple(entries[start:start + self._config.max_subs_per_sum])
                for start in range(0, len(entries), self._config.max_subs_per_sum)
            )
            if len(self._read_plan_cache) >= 32:
                self._read_plan_cache.pop(next(iter(self._read_plan_cache)))
            self._read_plan_cache[key] = plan

        decoded: list[tuple[PointScalar, Quality]] = []
        for chunk in plan:
            valid = [(point, decoder) for point, decoder in chunk if decoder is not None]
            values: list[tuple[PointScalar, Quality]] = []
            if valid:
                addresses = tuple((p.group, p.offset, p.size) for p, _ in valid)
                raw = await asyncio.to_thread(self._sum_read_bytes, addresses)
                expected = 4 * len(valid) + sum(p.size for p, _ in valid)
                if len(raw) < expected:
                    raise ProtocolError(
                        f"ADS sum read returned {len(raw)} bytes; expected at least {expected}"
                    )
                offset = 4 * len(valid)
                for index, (point, decoder) in enumerate(valid):
                    error = struct.unpack_from("<I", raw, index * 4)[0]
                    data = raw[offset:offset + point.size]
                    offset += point.size
                    if error:
                        values.append((None, Quality.BAD))
                        continue
                    try:
                        values.append((decoder(data), Quality.GOOD))
                    except (TypeError, ValueError, UnicodeDecodeError):
                        values.append((None, Quality.BAD))
            iterator = iter(values)
            for _, decoder in chunk:
                decoded.append(next(iterator) if decoder is not None else (None, Quality.BAD))

        by_point: dict[ADSPoint, list[tuple[PointScalar, Quality]]] = {}
        for point, value in zip(key, decoded, strict=True):
            by_point.setdefault(point, []).append(value)
        return tuple(by_point[point].pop() for point in points)

    async def resolve_symbol(self, symbol: str) -> tuple[int, int]:
        """按需查询 PLC 符号地址；同一 ADS session 内复用解析结果。"""
        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS resolve_symbol requires an active connection")
            group, offset, _ = await self._resolve_symbol_locked(symbol)
            return group, offset

    async def _resolve_symbol_locked(self, symbol: str) -> tuple[int, int, int | None]:
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached
        connection = self._connection
        if connection is None:
            raise ProtocolError("ADS connection is not available")
        info = await asyncio.to_thread(connection.get_symbol, symbol)
        group, offset = symbol_index_address(symbol, info)
        raw_size = getattr(info, "size", None)
        size = raw_size if type(raw_size) is int and raw_size > 0 else None
        resolved = (group, offset, size)
        self._symbol_cache[symbol] = resolved
        return resolved

    async def _resolve_configured_points(self) -> None:
        """连接建立后一次性解析当前点表所有符号，读写不再执行符号查询。"""
        resolved = dict(self._points)
        for point_id, mapped in resolved.items():
            if mapped.address_resolved:
                continue
            if mapped.symbol is None:
                raise ConfigError(f"ADS point '{point_id}' has no address")
            group, offset, plc_size = await self._resolve_symbol_locked(mapped.symbol)
            if mapped.data_type != "STRING" and plc_size is not None and plc_size != mapped.size:
                raise ConfigError(
                    f"ADS point '{point_id}': PLC size {plc_size} "
                    f"does not match {mapped.data_type} width {mapped.size}"
                )
            resolved[point_id] = mapped.resolved(index_group=group, index_offset=offset)
        self._points = resolved
        self._read_plan_cache.clear()

    def _invalidate_symbol_addresses(self) -> None:
        self._read_plan_cache.clear()
        self._symbol_cache.clear()
        self._points = {point_id: point.unresolved() for point_id, point in self._points.items()}

    def _mapped_point(self, point_id: str) -> ADSPoint:
        mapped = self._points.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection " f"'{self._point_table_id}'"
            )
        return mapped

    def _resolve_point(self, point: str) -> ADSPoint:
        """仅允许按当前 PointTable 已配置的 Key 寻址。"""
        if not isinstance(point, str):
            raise ConfigError("protocol point must be a configured point key")
        return self._mapped_point(point)

    def _sum_read_bytes(
        self,
        addresses: Sequence[tuple[int, int, int]],
    ) -> bytes:
        """通过 PyADS 公开 read_write 发送标准 ADS Sum Read 请求。

        请求体是连续 (index_group, index_offset, size) 的 12 字节记录，
        ADSIGRP_SUMUP_READ 的 index_offset 字段携带子请求数。
        """
        from ctypes import Structure, c_uint32

        from pyads.constants import ADSIGRP_SUMUP_READ

        connection = self._connection
        if connection is None:
            raise ProtocolError("ADS connection is not available")
        if not hasattr(connection, "read_write"):
            raise NotImplementedError("pyads Connection.read_write unavailable")

        class _SumReadItem(Structure):
            _fields_ = [
                ("iGroup", c_uint32),
                ("iOffset", c_uint32),
                ("size", c_uint32),
            ]

        request = (_SumReadItem * len(addresses))(
            *(_SumReadItem(group, offset, size) for group, offset, size in addresses)
        )
        response = connection.read_write(
            ADSIGRP_SUMUP_READ,
            len(addresses),
            None,
            request,
            None,
            check_length=False,
        )
        if response is None:
            raise ProtocolError("ADS Sum Read received no response")
        return bytes(response)

    async def _disconnect_after_failure(self) -> None:
        """传输失败后立即摘除运行资源。

        本方法可能在 Driver lock 内调用，因此不能等待订阅 callback drain；
        订阅清理转为独立 task，避免 callback 反向等待 Driver lock 形成死锁。
        """
        connection, subscriptions = self._detach_runtime()
        for subscription in subscriptions:
            task = asyncio.create_task(subscription.close())
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._on_cleanup_done)
        if connection is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.close)

    def _on_cleanup_done(self, task: asyncio.Task[None]) -> None:
        """取回后台订阅清理结果：异常如实记日志，不向事件循环泄漏。"""
        self._cleanup_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("ADS subscription cleanup failed: %s", exc)

    def _detach_runtime(
        self,
    ) -> tuple[Any, tuple[ADSSubscription, ...]]:
        """在 Driver lock 内原子摘除当前连接与订阅。"""
        connection, self._connection = self._connection, None
        subscriptions = tuple(self._subscriptions)
        self._subscriptions.clear()
        self._connected = False
        return connection, subscriptions

    async def _close_detached(
        self,
        connection: Any,
        subscriptions: tuple[ADSSubscription, ...],
    ) -> None:
        """在 Driver lock 外完成可能等待 callback 的资源清理。"""
        for subscription in subscriptions:
            await subscription.close()
        if connection is not None:
            try:
                async with asyncio.timeout(self._config.timeout):
                    await asyncio.to_thread(connection.close)
            except TimeoutError:
                logger.warning("ADS connection close exceeded %.3fs", self._config.timeout)
            except Exception:
                logger.exception("ADS connection close failed")

    def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()


def _encode_value(value: float | int | bool | str, point: ADSPoint) -> bytes:
    """Serialize one ADS scalar to exactly the addressed byte width."""
    if point.data_type == "STRING":
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
        if point.size <= 0 or len(encoded) >= point.size:
            raise ValueError(f"ADS STRING '{point.point_id}' exceeds configured size")
        return encoded.ljust(point.size, b"\x00")
    datatype = _plc_datatype(point.data_type)
    raw = bytes(datatype(value))
    if len(raw) != point.size:
        raise ValueError(f"ADS point '{point.point_id}' data width mismatch")
    return raw


def _decode_value(raw: bytes, point: ADSPoint) -> float | int | bool | str:
    if point.data_type == "STRING":
        return raw.split(b"\x00", 1)[0].decode("utf-8")
    datatype = _plc_datatype(point.data_type)
    value = datatype.from_buffer_copy(raw)
    scalar = value.value if hasattr(value, "value") else value
    result = _as_point_scalar(scalar)
    if result is None:
        raise TypeError("ADS scalar cannot be None")
    return result


def _as_point_scalar(
    value: object,
) -> float | int | bool | str | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported ADS value type '{type(value).__name__}'")


def _point_identities(points: Sequence[str]) -> tuple[str, ...]:
    """批量读返回样本的身份：str 输入即 point_id，动态点取其 point_id。"""
    return tuple(point if isinstance(point, str) else point.point_id for point in points)


def _bad_sample(point_id: str) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=None,
        timestamp=datetime.now(UTC),
        quality=Quality.BAD,
    )


def _is_point_level_error(exc: BaseException) -> bool:
    ads_error = getattr(pyads, "ADSError", None)
    return (
        ads_error is not None
        and isinstance(exc, ads_error)
        and getattr(exc, "err_code", None) == _ADSERR_SYMBOL_NOT_FOUND
    )


_ADS_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "SINT": (-128, 127),
    "USINT": (0, 255),
    "INT": (-32768, 32767),
    "UINT": (0, 65535),
    "DINT": (-2147483648, 2147483647),
    "UDINT": (0, 4294967295),
    "LINT": (-9223372036854775808, 9223372036854775807),
    "ULINT": (0, 18446744073709551615),
}


def _coerce_write_value(
    value: object,
    ads_type: str,
) -> float | int | bool | str:
    """把共享标量严格收敛到 ADS 基础类型。"""
    if ads_type == "BOOL":
        if type(value) is not bool:
            raise TypeError("ADS BOOL requires bool value")
        return value

    if ads_type == "STRING":
        if not isinstance(value, str):
            raise TypeError("ADS STRING requires string value")
        return value

    integer_range = _ADS_INTEGER_RANGES.get(ads_type)
    if integer_range is not None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"ADS {ads_type} requires integer-compatible value")
        integer = int(value)
        if float(value) != float(integer):
            raise ValueError(f"ADS {ads_type} requires an integer value")
        lower, upper = integer_range
        if not lower <= integer <= upper:
            raise ValueError(f"ADS {ads_type} value {integer} outside range {lower}..{upper}")
        return integer

    if ads_type in {"REAL", "LREAL"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"ADS {ads_type} requires numeric value")
        return float(value)

    raise ValueError(f"unsupported ADS type '{ads_type}'")


class _TrackedADSSubscription:
    """从 Driver 注册表中自动移除的订阅句柄。"""

    def __init__(
        self,
        owner: ADSDriver,
        subscription: ADSSubscription,
    ) -> None:
        self._owner = owner
        self._subscription = subscription
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner._subscriptions.discard(self._subscription)
        await self._subscription.close()
