"""基于 pyads 的共享 ADS ProtocolPort Adapter。

Driver 只实现 Collector 与 Commander 共同需要的单连接 connect/read/write/health。
持续订阅、采集节拍与重连策略属于具体应用，不进入 Shared Core。
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import struct
from collections.abc import Sequence
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
)
from core.domain import PointTable, ProtocolPoint

from .config import ADSConfig, parse_ads_config
from .mapping import ADSPoint, parse_ads_point

_ADSERR_SYMBOL_NOT_FOUND = 1808


def _pyads() -> Any:
    """延迟导入 pyads，把未类型化第三方对象隔离在 Adapter 内。"""
    try:
        import pyads  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ProtocolError(
            "ADS support requires the optional 'pyads' dependency"
        ) from exc
    return pyads


def _plc_datatype(ads_name: str) -> Any:
    pyads = _pyads()
    datatype = getattr(pyads, f"PLCTYPE_{ads_name}", None)
    if datatype is None:
        raise ConfigError(f"pyads does not expose PLCTYPE_{ads_name}")
    return datatype


class ADSDriver:
    """单个 DeviceConnection 的 ADS Driver。

    symbol 点在每次 ADS session 建立后解析为 index_group/index_offset 并缓存；
    read/write 不在周期路径上重复查询 symbol。
    """

    def __init__(
        self,
        connection: DeviceConnection,
        point_table: PointTable,
    ) -> None:
        if point_table.protocol.name != "ads":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'ads'"
            )

        self._connection_config = connection
        self._config: ADSConfig = parse_ads_config(connection)
        self._points = {
            point.point_id: parse_ads_point(point)
            for point in point_table.points.values()
        }

        self._lock = asyncio.Lock()
        self._connection: Any = None
        self._connected = False

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
                    raise ProtocolError(
                        f"ADS connection to {self._config.host} did not open"
                    )

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
                raise ProtocolError(
                    f"ADS connect failed for {self._config.host}: {exc}"
                ) from exc

            self._connected = True

    async def close(self) -> None:
        """关闭当前 ADS session；重复调用安全。"""
        async with self._lock:
            connection, self._connection = self._connection, None
            self._connected = False
            if connection is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(connection.close)

    def health(self) -> ConnectionHealth:
        """返回缓存连接状态，不执行 ADS wire 探测。"""
        if self._connected:
            return ConnectionHealth(
                healthy=True,
                message=(
                    f"connected to {self._config.host}:"
                    f"{self._config.target_port}"
                ),
            )
        return ConnectionHealth(healthy=False, message="not connected")

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        """按配置的 sum/sequential 策略读取点。"""
        if not points:
            return ()

        async with self._lock:
            if not self._connected or self._connection is None:
                raise ProtocolError("ADS read requires an active connection")
            try:
                if self._config.read_mode == "sum":
                    return await self._read_sum(points)
                return await self._read_sequential(points)
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"ADS read failed: {exc}") from exc

    async def write(
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

            results: list[ProtocolWriteResult] = []
            try:
                for write in writes:
                    mapped = self._mapped_point(write.point)
                    if not mapped.address_resolved:
                        results.append(
                            ProtocolWriteResult(
                                point_id=mapped.point_id,
                                success=False,
                                message="ADS address is unresolved",
                            )
                        )
                        continue

                    try:
                        datatype = _plc_datatype(mapped.data_type)
                    except ConfigError as exc:
                        results.append(
                            ProtocolWriteResult(
                                point_id=mapped.point_id,
                                success=False,
                                message=str(exc),
                            )
                        )
                        continue

                    await asyncio.to_thread(
                        self._connection.write,
                        mapped.index_group,
                        mapped.index_offset,
                        write.value,
                        datatype,
                    )
                    results.append(
                        ProtocolWriteResult(
                            point_id=mapped.point_id,
                            success=True,
                        )
                    )
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"ADS write failed: {exc}") from exc

            return tuple(results)

    async def _read_sum(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        results: list[ProtocolSample | None] = [None] * len(points)
        fixed: list[tuple[int, ADSPoint]] = []
        variable: list[tuple[int, ADSPoint]] = []

        for index, point in enumerate(points):
            mapped = self._mapped_point(point)
            if not mapped.address_resolved:
                results[index] = _bad_sample(point.point_id)
            elif mapped.size > 0:
                fixed.append((index, mapped))
            else:
                variable.append((index, mapped))

        max_subs = self._config.max_subs_per_sum
        for start in range(0, len(fixed), max_subs):
            chunk = fixed[start : start + max_subs]
            addresses = [
                (
                    mapped.index_group,
                    mapped.index_offset,
                    mapped.size,
                )
                for _, mapped in chunk
            ]
            raw = await asyncio.to_thread(
                self._sum_read_bytes,
                addresses,
            )
            expected = 4 * len(chunk) + sum(
                mapped.size for _, mapped in chunk
            )
            if len(raw) < expected:
                raise ProtocolError(
                    f"ADS sum read returned {len(raw)} bytes; "
                    f"expected at least {expected}"
                )

            data_offset = 4 * len(chunk)
            for item_index, (result_index, mapped) in enumerate(chunk):
                error = struct.unpack_from(
                    "<I",
                    raw,
                    item_index * 4,
                )[0]
                value_bytes = raw[
                    data_offset : data_offset + mapped.size
                ]
                data_offset += mapped.size
                if error:
                    results[result_index] = _bad_sample(mapped.point_id)
                    continue
                try:
                    value = _decode_value(value_bytes, mapped)
                except (TypeError, ValueError, UnicodeDecodeError):
                    results[result_index] = _bad_sample(mapped.point_id)
                    continue
                results[result_index] = ProtocolSample(
                    point_id=mapped.point_id,
                    value=value,
                    quality=Quality.GOOD,
                )

        for result_index, mapped in variable:
            try:
                value = await asyncio.to_thread(
                    self._connection.read,
                    mapped.index_group,
                    mapped.index_offset,
                    _plc_datatype(mapped.data_type),
                )
            except Exception as exc:
                if _is_point_level_error(exc):
                    results[result_index] = _bad_sample(mapped.point_id)
                    continue
                raise
            results[result_index] = ProtocolSample(
                point_id=mapped.point_id,
                value=_as_point_scalar(value),
                quality=Quality.GOOD,
            )

        if any(result is None for result in results):
            raise RuntimeError("ADS read did not produce a result for every point")
        return tuple(
            result
            for result in results
            if result is not None
        )

    async def _read_sequential(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        semaphore = asyncio.Semaphore(
            self._config.max_concurrent_reads
        )

        async def read_one(point: ProtocolPoint) -> ProtocolSample:
            mapped = self._mapped_point(point)
            if not mapped.address_resolved:
                return _bad_sample(mapped.point_id)
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
                    return _bad_sample(mapped.point_id)
                raise
            return ProtocolSample(
                point_id=mapped.point_id,
                value=_as_point_scalar(value),
                quality=Quality.GOOD,
            )

        return tuple(
            await asyncio.gather(
                *(read_one(point) for point in points)
            )
        )

    async def _resolve_symbols(self) -> None:
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
        self._points = {
            point_id: point.unresolved()
            for point_id, point in self._points.items()
        }

    def _mapped_point(self, point: ProtocolPoint) -> ADSPoint:
        mapped = self._points.get(point.point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point.point_id}' is not part of connection "
                f"'{self._connection_config.connection_id}' point table"
            )
        return mapped

    def _sum_read_bytes(
        self,
        addresses: list[tuple[int, int, int]],
    ) -> bytes:
        """调用 pyads 地址型 Sum Read。"""
        try:
            from pyads.pyads_ex import adsSumReadBytes  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ProtocolError(
                "installed pyads does not expose adsSumReadBytes"
            ) from exc

        connection = self._connection
        if connection is None:
            raise ProtocolError("ADS connection is not available")
        return bytes(
            adsSumReadBytes(
                connection._port,
                connection._adr,
                addresses,
            )
        )

    def _signal_disconnect(self) -> None:
        self._connected = False
        self._close_connection()

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
    raise TypeError(
        f"unsupported ADS value type '{type(value).__name__}'"
    )


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
