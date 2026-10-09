"""基于 pyads 的共享 ADS ProtocolPort Adapter。

Driver 实现共享的 connect/read/write/health，并通过可选订阅能力暴露 ADS
device-notification。何时订阅、订阅哪些点以及失败后的应用级重建策略仍由上层决定。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import struct
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from core.application.errors import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .config import ADSConfig, parse_ads_config
from .mapping import ADSPoint, parse_ads_point
from .subscription import ADSSubscription

_ADSERR_SYMBOL_NOT_FOUND = 1808


def _pyads() -> Any:
    """延迟导入 pyads，把未类型化第三方对象隔离在 Adapter 内。"""
    try:
        import pyads  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ProtocolError("ADS support requires the optional 'pyads' dependency") from exc
    return pyads


def _plc_datatype(ads_name: str) -> Any:
    pyads = _pyads()
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
        # 地址解析与读取分组均绑定当前 ADS session；断连/重连后失效。
        self._read_plan_cache: dict[
            tuple[str, ...],
            tuple[
                tuple[tuple[tuple[int, ADSPoint], ...], tuple[tuple[int, int, int], ...], int],
                ...,
            ],
        ] = {}
        self._read_variable_cache: dict[tuple[str, ...], tuple[tuple[int, ADSPoint], ...]] = {}
        self._read_unresolved_cache: dict[tuple[str, ...], tuple[int, ...]] = {}
        self._points: dict[str, ADSPoint] = {}
        self.update_point_table(point_table)

    def update_point_table(self, point_table: PointTable) -> None:
        """热重载点表：重建符号/类型映射并失效读取缓存（不断开连接）。

        进行中的 ADS 订阅仍按旧 symbol 通知，由上层重启订阅后按新映射
        重新注册。
        """
        self._point_table_id = point_table.point_table_id
        self._points = {
            point.point_id: parse_ads_point(point) for point in point_table.points.values()
        }
        self._read_plan_cache.clear()
        self._read_variable_cache.clear()
        self._read_unresolved_cache.clear()

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

    async def connect(self) -> None:
        """建立一次 ADS session 并解析全部 symbol 地址。"""
        async with self._lock:
            if self._connected:
                return

            pyads = _pyads()
            self._close_connection()
            net_id = self._config.target_net_id or None
            connection = pyads.Connection(
                net_id,
                self._config.target_port,
                self._config.host,
            )
            connection.set_timeout(int(self._config.timeout * 1000))

            try:
                await asyncio.to_thread(connection.open)
                if not connection.is_open:
                    raise ProtocolError(f"ADS connection to {self._config.host} did not open")

                self._connection = connection
                self._invalidate_symbol_addresses()
                await self._resolve_symbols()
            except ProtocolError:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(connection.close)
                self._connection = None
                self._connected = False
                raise
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(connection.close)
                self._connection = None
                self._connected = False
                raise ProtocolError(f"ADS connect failed for {self._config.host}: {exc}") from exc

            self._connected = True

    async def close(self) -> None:
        """关闭当前 ADS session；重复调用安全。"""
        async with self._lock:
            connection, subscriptions = self._detach_runtime()

        await self._close_detached(connection, subscriptions)

    def health(self) -> ConnectionHealth:
        """返回缓存连接状态，不执行 ADS wire 探测。"""
        if self._connected:
            return ConnectionHealth(
                healthy=True,
                message=(f"connected to {self._config.host}:" f"{self._config.target_port}"),
            )
        return ConnectionHealth(healthy=False, message="not connected")

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

    async def read_one(self, point_id: str) -> ProtocolSample:
        """读取一个逻辑点；低频路径，不走 Sum Read、分段或读取分组缓存。

        点级失败（symbol 不存在等）返回 BAD 质量样本；连接/传输级
        失败统一标记断线并抛 ProtocolError。
        """
        mapped = self._mapped_point(point_id)
        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS read requires an active connection")
            if not mapped.address_resolved:
                return _bad_sample(point_id)
            try:
                value = await asyncio.to_thread(
                    self._connection.read,
                    mapped.index_group,
                    mapped.index_offset,
                    _plc_datatype(mapped.data_type),
                )
            except Exception as exc:
                if _is_point_level_error(exc):
                    return _bad_sample(point_id)
                await self._disconnect_after_failure()
                raise ProtocolError(f"ADS read failed: {exc}") from exc
            try:
                scalar = _as_point_scalar(value)
            except TypeError:
                return _bad_sample(point_id)
            return ProtocolSample(point_id=point_id, value=scalar, quality=Quality.GOOD)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        """写入一个逻辑点；低频路径，不使用 Sum Write 批量规划。

        配置/取值级问题返回失败结果；连接/传输级问题统一抛 ProtocolError。
        """
        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS write requires an active connection")
            try:
                return await self._write_single_point(write)
            except Exception as exc:
                await self._disconnect_after_failure()
                raise ProtocolError(f"ADS write failed: {exc}") from exc

    async def read_many(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        """按配置的 sum/sequential 策略批量读取，返回按输入顺序排列的样本元组。"""
        raw = await self._read_values(point_ids)
        return tuple(
            ProtocolSample(point_id=pid, value=value, quality=quality)
            for pid, (value, quality) in zip(point_ids, raw, strict=True)
        )

    async def _read_values(
        self,
        point_ids: Sequence[str],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """按配置的 sum/sequential 策略读取点，返回原始值及逐点质量。"""
        if not point_ids:
            return ()

        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS read requires an active connection")
            try:
                if self._config.read_mode == "sum":
                    mapped = tuple(self._mapped_point(pid) for pid in point_ids)
                    if (
                        len(mapped) > 1
                        and all(point.symbol is not None and point.address_resolved
                                for point in mapped)
                        and hasattr(self._connection, "read_list_by_name")
                    ):
                        return await self._read_symbol_list_raw(mapped)
                    return await self._read_sum_raw(point_ids)
                return await self._read_sequential_raw(point_ids)
            except ProtocolError:
                raise
            except Exception as exc:
                await self._disconnect_after_failure()
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

            # Only a unique, fully symbol-addressed batch may use pyads Sum Write.
            # Index-only or mixed requests retain the existing ordered path.
            mapped_batch = [self._mapped_point(write.point_id) for write in writes]
            symbols = [point.symbol for point in mapped_batch]
            if (
                len(writes) > 1
                and all(point.address_resolved for point in mapped_batch)
                and all(symbol is not None for symbol in symbols)
                and len(set(symbols)) == len(symbols)
                and self._disjoint_ads_addresses(mapped_batch)
                and hasattr(self._connection, "write_list_by_name")
            ):
                prepared: dict[str, object] = {}
                try:
                    for write, point in zip(writes, mapped_batch, strict=True):
                        _plc_datatype(point.data_type)
                        assert point.symbol is not None
                        prepared[point.symbol] = _coerce_write_value(
                            write.value, point.data_type
                        )
                except (ConfigError, TypeError, ValueError):
                    # Preserve existing per-point validation and error isolation.
                    pass
                else:
                    try:
                        responses = await asyncio.to_thread(
                            self._connection.write_list_by_name,
                            prepared,
                            ads_sub_commands=self._config.max_subs_per_sum,
                        )
                    except Exception as exc:
                        await self._disconnect_after_failure()
                        raise ProtocolError(f"ADS sum write failed: {exc}") from exc
                    return tuple(
                        ProtocolWriteResult(
                            point_id=write.point_id,
                            success=responses.get(point.symbol) in (0, "no error"),
                            message=(
                                None
                                if responses.get(point.symbol) in (0, "no error")
                                else (
                                    "ADS sum write: "
                                    f"{responses.get(point.symbol, 'missing status')}"
                                )
                            ),
                        )
                        for write, point in zip(writes, mapped_batch, strict=True)
                    )

            results: list[ProtocolWriteResult] = []
            try:
                for write in writes:
                    results.append(await self._write_single_point(write))
            except Exception as exc:
                await self._disconnect_after_failure()
                raise ProtocolError(f"ADS write failed: {exc}") from exc

            return tuple(results)

    async def _write_single_point(self, write: ProtocolWrite) -> ProtocolWriteResult:
        """按已解析 index 地址写入单点；配置/取值级问题收敛为单点失败。

        连接/传输级异常原样抛出，由调用方统一断线处理。
        """
        mapped = self._mapped_point(write.point_id)
        if not mapped.address_resolved:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                success=False,
                message="ADS address is unresolved",
            )

        try:
            datatype = _plc_datatype(mapped.data_type)
        except ConfigError as exc:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                success=False,
                message=str(exc),
            )

        try:
            raw_value = _coerce_write_value(
                write.value,
                mapped.data_type,
            )
        except (TypeError, ValueError) as exc:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                success=False,
                message=str(exc),
            )

        await asyncio.to_thread(
            self._connection.write,
            mapped.index_group,
            mapped.index_offset,
            raw_value,
            datatype,
        )
        return ProtocolWriteResult(
            point_id=mapped.point_id,
            success=True,
        )

    async def interrogate(self) -> None:
        """ADS 不支持 IEC104 式总召能力。"""
        raise ProtocolCapabilityError("ads does not support interrogation")

    @staticmethod
    def _disjoint_ads_addresses(points: Sequence[ADSPoint]) -> bool:
        """Reject overlapping address spans, including differently named aliases."""
        spans: dict[int, list[tuple[int, int]]] = {}
        for point in points:
            if point.size <= 0:
                # Variable-sized symbols cannot be verified for overlap.
                return False
            start, end = point.index_offset, point.index_offset + point.size
            region = spans.setdefault(point.index_group, [])
            if any(start < other_end and other_start < end
                   for other_start, other_end in region):
                return False
            region.append((start, end))
        return True

    async def _read_symbol_list_raw(
        self,
        points: tuple[ADSPoint, ...],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """使用 pyads 公开 Sum Read；保留请求位置和重复 symbol。"""
        connection = self._connection
        if connection is None:
            raise ProtocolError("ADS connection is not available")
        symbols = list(dict.fromkeys(point.symbol for point in points))
        result = await asyncio.to_thread(
            connection.read_list_by_name,
            symbols,
            ads_sub_commands=self._config.max_subs_per_sum,
        )
        values: list[tuple[PointScalar, Quality]] = []
        for point in points:
            raw = result.get(point.symbol)
            # pyads represents per-symbol ADS errors as text. A non-string
            # PLC type receiving text is an error, not a valid GOOD sample.
            if point.data_type != "STRING" and isinstance(raw, str):
                values.append((None, Quality.BAD))
                continue
            if raw is None:
                values.append((None, Quality.BAD))
                continue
            try:
                values.append((_as_point_scalar(raw), Quality.GOOD))
            except TypeError:
                values.append((None, Quality.BAD))
        return tuple(values)

    async def _read_sum_raw(
        self,
        point_ids: Sequence[str],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        key = tuple(point_ids)
        cached = self._read_plan_cache.get(key)
        if cached is None:
            unresolved: list[int] = []
            fixed: list[tuple[int, ADSPoint]] = []
            variable: list[tuple[int, ADSPoint]] = []
            for index, point_id in enumerate(key):
                mapped = self._mapped_point(point_id)
                if not mapped.address_resolved:
                    unresolved.append(index)
                elif mapped.size > 0:
                    fixed.append((index, mapped))
                else:
                    variable.append((index, mapped))
            max_subs = self._config.max_subs_per_sum
            chunks = []
            for start in range(0, len(fixed), max_subs):
                chunk = tuple(fixed[start : start + max_subs])
                addresses = tuple(
                    (mapped.index_group, mapped.index_offset, mapped.size) for _, mapped in chunk
                )
                expected = 4 * len(chunk) + sum(mapped.size for _, mapped in chunk)
                chunks.append((chunk, addresses, expected))
            cached = tuple(chunks)
            if len(self._read_plan_cache) >= 32:
                old = next(iter(self._read_plan_cache))
                self._read_plan_cache.pop(old)
                self._read_variable_cache.pop(old, None)
                self._read_unresolved_cache.pop(old, None)
            self._read_plan_cache[key] = cached
            self._read_variable_cache[key] = tuple(variable)
            self._read_unresolved_cache[key] = tuple(unresolved)

        unresolved_indexes = self._read_unresolved_cache.get(key, ())
        variable_points = self._read_variable_cache.get(key, ())
        results: list[tuple[PointScalar, Quality] | None] = [None] * len(key)
        for index in unresolved_indexes:
            results[index] = (None, Quality.BAD)
        for chunk, addresses, expected in cached:
            try:
                raw = await asyncio.to_thread(
                    self._sum_read_bytes,
                    addresses,
                )
            except NotImplementedError:
                # Current pyads lacks public Index-based Sum Read. If its
                # internal connection handles disappear, retain correctness
                # using the supported per-address read API.
                return await self._read_sequential_raw(point_ids)
            if len(raw) < expected:
                raise ProtocolError(
                    f"ADS sum read returned {len(raw)} bytes; " f"expected at least {expected}"
                )

            data_offset = 4 * len(chunk)
            for item_index, (result_index, mapped) in enumerate(chunk):
                error = struct.unpack_from(
                    "<I",
                    raw,
                    item_index * 4,
                )[0]
                value_bytes = raw[data_offset : data_offset + mapped.size]
                data_offset += mapped.size
                if error:
                    results[result_index] = (None, Quality.BAD)
                    continue
                try:
                    value = _decode_value(value_bytes, mapped)
                except (TypeError, ValueError, UnicodeDecodeError):
                    results[result_index] = (None, Quality.BAD)
                    continue
                results[result_index] = (value, Quality.GOOD)

        for result_index, mapped in variable_points:
            try:
                value = await asyncio.to_thread(
                    self._connection.read,
                    mapped.index_group,
                    mapped.index_offset,
                    _plc_datatype(mapped.data_type),
                )
            except Exception as exc:
                if _is_point_level_error(exc):
                    results[result_index] = (None, Quality.BAD)
                    continue
                raise
            results[result_index] = (_as_point_scalar(value), Quality.GOOD)

        if any(result is None for result in results):
            raise RuntimeError("ADS read did not produce a result for every point")
        return tuple(result for result in results if result is not None)

    async def _read_sequential_raw(
        self,
        point_ids: Sequence[str],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        semaphore = asyncio.Semaphore(self._config.max_concurrent_reads)

        async def read_one(point_id: str) -> tuple[PointScalar, Quality]:
            mapped = self._mapped_point(point_id)
            if not mapped.address_resolved:
                return (None, Quality.BAD)
            try:
                async with semaphore:
                    value = await asyncio.to_thread(
                        self._connection.read,
                        mapped.index_group,
                        mapped.index_offset,
                        _plc_datatype(mapped.data_type),
                    )
            except Exception as exc:
                if _is_point_level_error(exc):
                    return (None, Quality.BAD)
                raise
            return (_as_point_scalar(value), Quality.GOOD)

        return tuple(await asyncio.gather(*(read_one(point_id) for point_id in point_ids)))

    async def _resolve_symbols(self) -> None:
        self._read_plan_cache.clear()
        self._read_variable_cache.clear()
        self._read_unresolved_cache.clear()
        """在当前 session 内一次性解析 symbol -> index 地址。"""
        connection = self._connection
        if connection is None:
            raise ProtocolError("ADS connection is not available")

        for point_id, point in tuple(self._points.items()):
            if point.symbol is None:
                continue
            try:
                symbol = await asyncio.to_thread(
                    connection.get_symbol,
                    point.symbol,
                )
            except Exception as exc:
                if _is_point_level_error(exc):
                    continue
                raise

            index_group = getattr(symbol, "index_group", None)
            index_offset = getattr(symbol, "index_offset", None)
            if (
                isinstance(index_group, bool)
                or not isinstance(index_group, int)
                or isinstance(index_offset, bool)
                or not isinstance(index_offset, int)
            ):
                continue

            size = point.size
            plc_type = getattr(symbol, "plc_type", None)
            if plc_type is not None:
                with contextlib.suppress(TypeError):
                    size = ctypes.sizeof(plc_type)

            self._points[point_id] = point.resolved(
                index_group=index_group,
                index_offset=index_offset,
                size=size,
            )

    def _invalidate_symbol_addresses(self) -> None:
        self._read_plan_cache.clear()
        self._read_variable_cache.clear()
        self._read_unresolved_cache.clear()
        self._points = {point_id: point.unresolved() for point_id, point in self._points.items()}

    def _mapped_point(self, point_id: str) -> ADSPoint:
        mapped = self._points.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection " f"'{self._point_table_id}'"
            )
        return mapped

    def _sum_read_bytes(
        self,
        addresses: Sequence[tuple[int, int, int]],
    ) -> bytes:
        """通过 PyADS 公开 read_write 发送标准 ADS Sum Read 请求。

        请求体是连续 (index_group, index_offset, size) 的 12 字节记录，
        ADSIGRP_SUMUP_READ 的 index_offset 字段携带子请求数。
        """
        from ctypes import Structure, c_uint32

        from pyads.constants import ADSIGRP_SUMUP_READ  # type: ignore[import-untyped]

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
            asyncio.create_task(subscription.close())
        if connection is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.close)

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
            with contextlib.suppress(Exception):
                await asyncio.to_thread(connection.close)

    def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()


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


def _bad_sample(point_id: str) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=None,
        quality=Quality.BAD,
    )


def _is_point_level_error(exc: BaseException) -> bool:
    pyads = _pyads()
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
