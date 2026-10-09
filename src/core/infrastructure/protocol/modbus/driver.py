"""基于 pymodbus 的共享 Modbus TCP ProtocolPort Adapter。"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Any

from core.application.errors import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.port import ProtocolSampleCallback, SubscriptionHandle
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

from .config import ModbusConfig, parse_modbus_config
from .mapping import ModbusPoint, group_consecutive_reads, parse_modbus_point

if TYPE_CHECKING:
    from pymodbus.client import AsyncModbusTcpClient, ModbusTcpClient
else:
    try:
        from pymodbus.client import AsyncModbusTcpClient, ModbusTcpClient
    except ImportError:  # pymodbus 是可选依赖；缺失时不影响其它协议模块导入。
        AsyncModbusTcpClient = None
        ModbusTcpClient = None

_PYMODBUS_MISSING = "Modbus support requires the optional 'pymodbus' dependency"

_BIT_TYPES = frozenset({"coil", "discrete_input"})
_READ_ONLY_TYPES = frozenset({"discrete_input", "input"})
_MULTI_REGISTER_FMT: dict[str, str] = {
    "int32": ">i",
    "uint32": ">I",
    "float32": ">f",
    "int64": ">q",
    "uint64": ">Q",
    "float64": ">d",
}
_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "int8": (-128, 127),
    "uint8": (0, 255),
    "int16": (-32768, 32767),
    "uint16": (0, 65535),
    "int32": (-2147483648, 2147483647),
    "uint32": (0, 4294967295),
    "int64": (-9223372036854775808, 9223372036854775807),
    "uint64": (0, 18446744073709551615),
}
_DECODE_FAILED = object()


@dataclass(frozen=True, slots=True)
class _ReadGroupPlan:
    points: tuple[ModbusPoint, ...]
    register_type: str
    start: int
    count: int


class ModbusDriver:
    """单个 Endpoint 的 Modbus TCP Driver。

    Driver 只执行单次 connect/read/write，不负责重连退避或采集调度。一个实例
    由单一 asyncio event loop 持有，内部 Lock 防止同一 client 上读写交错。
    """

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        if point_table.protocol.name != "modbus":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'modbus'"
            )

        self._point_table_id = point_table.point_table_id
        self._config: ModbusConfig = parse_modbus_config(
            endpoint,
            protocol_options,
        )
        # 相同选点序列复用预编译读取组；点表热更新时整体失效。
        self._read_plan_cache: dict[tuple[str, ...], tuple[_ReadGroupPlan, ...]] = {}
        self._points: dict[str, ModbusPoint] = {}
        self.update_point_table(point_table)
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._connected = False

    def capabilities(self) -> frozenset[ProtocolCapability]:
        """返回 Modbus Driver 实际支持的协议能力。"""
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
            }
        )

    def update_point_table(self, point_table: PointTable) -> None:
        """热重载点表：重建地址/字序映射并失效读取分组缓存（不断开连接）。"""
        self._points = {
            point.point_id: parse_modbus_point(
                point,
                default_word_order=self._config.word_order,
            )
            for point in point_table.points.values()
        }
        self._read_plan_cache.clear()

    async def connect(self) -> None:
        """建立一次 Modbus TCP 连接；不在 Driver 内部重试。"""
        async with self._lock:
            if self._connected:
                return

            if AsyncModbusTcpClient is None:
                raise ProtocolError(_PYMODBUS_MISSING)

            self._close_client()
            client = AsyncModbusTcpClient(
                self._config.host,
                port=self._config.port,
                timeout=self._config.timeout,
                retries=0,
            )
            try:
                connected = await client.connect()
            except Exception as exc:
                with contextlib.suppress(Exception):
                    client.close()
                raise ProtocolError(
                    f"Modbus connect failed for {self._config.host}:" f"{self._config.port}: {exc}"
                ) from exc

            if not connected:
                with contextlib.suppress(Exception):
                    client.close()
                raise ProtocolError(
                    f"Modbus server rejected connection at "
                    f"{self._config.host}:{self._config.port}"
                )

            self._client = client
            self._connected = True

    async def close(self) -> None:
        """关闭 Modbus client；重复调用安全。"""
        async with self._lock:
            self._close_client()
            self._connected = False

    def health(self) -> ConnectionHealth:
        """返回缓存连接状态，不执行网络探测。"""
        if self._connected:
            return ConnectionHealth(
                healthy=True,
                message=f"connected to {self._config.host}:{self._config.port}",
            )
        return ConnectionHealth(healthy=False, message="not connected")

    async def read_one(self, point_id: str) -> ProtocolSample:
        """读取一个逻辑点；低频路径，不走批量分组规划或缓存。

        一个逻辑点可能占多个寄存器（如 float32 占 2 个），但仍只发送
        一次 Modbus 读取请求。解码失败返回 BAD 质量样本；Modbus 异常
        响应与通信错误分别抛出 ProtocolError，只有通信错误标记断线。
        """
        point = self._mapped_point(point_id)
        async with self._lock:
            if not self._connected:
                raise ProtocolError("Modbus read requires an active connection")
            try:
                response = await self._read_request(
                    point.register_type, point.address, point.count
                )
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"Modbus read failed: {exc}") from exc

            if response.isError():
                raise ProtocolError(
                    f"Modbus {point.register_type} read at {point.address} "
                    f"count={point.count} returned an exception response"
                )

            raw: Sequence[object] = (
                response.bits
                if point.register_type in _BIT_TYPES
                else response.registers
            )
            try:
                value = _decode_point(point, raw[: point.count])
            except (IndexError, TypeError, ValueError, struct.error):
                return ProtocolSample(point_id=point_id, value=None, quality=Quality.BAD)
            return ProtocolSample(
                point_id=point_id,
                value=_as_point_scalar(value),
                quality=Quality.GOOD,
            )

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        """写入一个逻辑点；低频路径，不做多点合并或写规划。

        一个逻辑点最多触发一次实际写请求：coil 用 write_coil，单寄存器
        用 write_register，多寄存器逻辑点用一次 write_registers(FC16)。
        配置或取值不合法时不向设备发送任何请求。
        """
        point = self._mapped_point(write.point_id)
        if point.register_type in _READ_ONLY_TYPES:
            return ProtocolWriteResult(
                point_id=write.point_id,
                success=False,
                message=f"{point.register_type} is read-only",
            )
        async with self._lock:
            if not self._connected:
                raise ProtocolError("Modbus write requires an active connection")
            try:
                accepted = await self._write_single(point, write.value)
            except (TypeError, ValueError, struct.error) as exc:
                return ProtocolWriteResult(
                    point_id=write.point_id,
                    success=False,
                    message=str(exc) or type(exc).__name__,
                )
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"Modbus write failed: {exc}") from exc
        if not accepted:
            return ProtocolWriteResult(
                point_id=write.point_id,
                success=False,
                message="Modbus exception response",
            )
        return ProtocolWriteResult(point_id=write.point_id, success=True)

    async def read(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        """兼容旧接口；统一转发至 read_many。"""
        return await self.read_many(point_ids)

    async def read_many(
        self,
        point_ids: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        """兼容标准协议端口；由调用方选择是否需要 DTO 封装。"""
        values = await self.read_raw(point_ids)
        return tuple(
            ProtocolSample(point_id=point_id, value=value, quality=quality)
            for point_id, (value, quality) in zip(point_ids, values, strict=True)
        )

    async def read_raw(
        self,
        point_ids: Sequence[str],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """读取原始数据及逐点质量；不创建 ProtocolSample。"""
        if not point_ids:
            return ()

        async with self._lock:
            if not self._connected:
                raise ProtocolError("Modbus read requires an active connection")

            plan = self._read_plan(point_ids)
            try:
                values: dict[str, object] = {}
                for group in plan:
                    values.update(await self._read_group(group))
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"Modbus read failed: {exc}") from exc

            return tuple(
                (None, Quality.BAD)
                if values.get(point_id, _DECODE_FAILED) is _DECODE_FAILED
                else (_as_point_scalar(values[point_id]), Quality.GOOD)
                for point_id in point_ids
            )

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """兼容旧接口；统一转发至 write_many。"""
        return await self.write_many(writes)

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """Modbus 不支持多逻辑点批量写入；请逐点调用 write_one。"""
        raise NotImplementedError("Modbus write_many is not implemented")

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        """Modbus TCP 不支持协议级主动订阅。"""
        del point_ids, callback, interval
        raise ProtocolCapabilityError("modbus does not support subscription")

    async def interrogate(self) -> None:
        """Modbus TCP 不支持 IEC104 式总召能力。"""
        raise ProtocolCapabilityError("modbus does not support interrogation")

    def _mapped_point(self, point_id: str) -> ModbusPoint:
        mapped = self._points.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection " f"'{self._point_table_id}'"
            )
        return mapped

    def _read_plan(self, point_ids: Sequence[str]) -> tuple[_ReadGroupPlan, ...]:
        """按点位有序序列缓存寄存器分组；命中时跳过映射及分组合并。"""
        key = tuple(point_ids)
        cached = self._read_plan_cache.get(key)
        if cached is not None:
            return cached
        mapped = [self._mapped_point(point_id) for point_id in key]
        plan = tuple(
            _ReadGroupPlan(
                points=tuple(group),
                register_type=group[0].register_type,
                start=min(point.address for point in group),
                count=max(point.address + point.count for point in group)
                - min(point.address for point in group),
            )
            for group in group_consecutive_reads(mapped)
        )
        # 典型连续轮询通常只有一个 key；动态请求限制内存增长。
        if len(self._read_plan_cache) >= 32:
            self._read_plan_cache.pop(next(iter(self._read_plan_cache)))
        self._read_plan_cache[key] = plan
        return plan

    async def _read_request(
        self,
        register_type: str,
        start: int,
        count: int,
    ) -> Any:
        """发送一次 Modbus 读取请求并返回原始响应；不检查 isError。"""
        client = self._client
        unit_id = self._config.unit_id
        if register_type == "coil":
            return await client.read_coils(start, count=count, device_id=unit_id)
        if register_type == "discrete_input":
            return await client.read_discrete_inputs(start, count=count, device_id=unit_id)
        if register_type == "holding":
            return await client.read_holding_registers(start, count=count, device_id=unit_id)
        return await client.read_input_registers(start, count=count, device_id=unit_id)

    async def _read_group(
        self,
        group: _ReadGroupPlan,
    ) -> dict[str, object]:
        register_type = group.register_type
        start = group.start
        count = group.count

        response = await self._read_request(register_type, start, count)

        if response.isError():
            raise ProtocolError(
                f"Modbus {register_type} read at {start} count={count} "
                "returned an exception response"
            )

        # 协议响应的序列只读取不修改，避免每轮复制整块寄存器。
        raw: Sequence[object]
        raw = response.bits if register_type in _BIT_TYPES else response.registers

        values: dict[str, object] = {}
        for point in group.points:
            offset = point.address - start
            segment = raw[offset : offset + point.count]
            try:
                values[point.point_id] = _decode_point(point, segment)
            except (IndexError, TypeError, ValueError, struct.error):
                values[point.point_id] = _DECODE_FAILED
        return values

    async def _write_single(
        self,
        point: ModbusPoint,
        value: object,
    ) -> bool:
        client = self._client
        unit_id = self._config.unit_id

        if point.register_type == "coil":
            coil_value = _encode_coil(value, point.data_type)
            response = await client.write_coil(
                point.address,
                coil_value,
                device_id=unit_id,
            )
        else:
            words = _encode_registers(
                value,
                point.data_type,
                point.word_order,
            )
            if len(words) == 1:
                response = await client.write_register(
                    point.address,
                    words[0],
                    device_id=unit_id,
                )
            else:
                response = await client.write_registers(
                    point.address,
                    words,
                    device_id=unit_id,
                )

        return not response.isError()

    def _signal_disconnect(self) -> None:
        self._connected = False
        self._close_client()

    def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _decode_point(
    point: ModbusPoint,
    segment: Sequence[object],
) -> object:
    if not segment:
        raise ValueError("empty Modbus response segment")

    if point.register_type in _BIT_TYPES:
        raw = segment[0]
        if point.data_type == "bool":
            return bool(raw)
        if isinstance(raw, bool):
            return int(raw)
        if isinstance(raw, int):
            return raw
        raise TypeError("Modbus bit response is not boolean/integer")

    registers = [_strict_register(value) for value in segment]
    return _decode_registers(registers, point.data_type, point.word_order)


def _decode_registers(
    registers: list[int],
    data_type: str,
    word_order: str,
) -> object:
    first = registers[0]
    if data_type == "bool":
        return bool(first & 0x01)
    if data_type == "int8":
        value = first & 0xFF
        return value if value < 0x80 else value - 0x100
    if data_type == "uint8":
        return first & 0xFF
    if data_type == "int16":
        return first if first < 0x8000 else first - 0x10000
    if data_type == "uint16":
        return first

    fmt = _MULTI_REGISTER_FMT.get(data_type)
    if fmt is None:
        raise ValueError(f"unsupported Modbus data type '{data_type}'")

    # 通用 32/64 位编解码交由 PyModbus，避免自行拼接二进制字节流。
    if ModbusTcpClient is None:
        raise ProtocolError(_PYMODBUS_MISSING)
    datatype = getattr(ModbusTcpClient.DATATYPE, data_type.upper(), None)
    if datatype is None:
        raise ValueError(f"unsupported PyModbus data type '{data_type}'")
    return ModbusTcpClient.convert_from_registers(
        registers,
        datatype,
        word_order="little" if word_order == "little_endian" else "big",
    )


def _encode_registers(
    value: object,
    data_type: str,
    word_order: str,
) -> list[int]:
    if data_type == "bool":
        if type(value) is not bool:
            raise TypeError("boolean Modbus point requires bool value")
        return [1 if value else 0]

    integer_range = _INTEGER_RANGES.get(data_type)
    if integer_range is not None:
        number: int | float = _strict_integer_value(value, data_type, integer_range)
        if data_type in {"int8", "uint8", "int16", "uint16"}:
            return [int(number) & 0xFFFF]
    elif data_type in {"float32", "float64"}:
        number = _strict_float_value(value, data_type)
    else:
        raise ValueError(f"unsupported Modbus data type '{data_type}'")

    if ModbusTcpClient is None:
        raise ProtocolError(_PYMODBUS_MISSING)
    datatype = getattr(ModbusTcpClient.DATATYPE, data_type.upper(), None)
    if datatype is None:
        raise ValueError(f"unsupported PyModbus data type '{data_type}'")
    return ModbusTcpClient.convert_to_registers(
        number,
        datatype,
        word_order="little" if word_order == "little_endian" else "big",
    )


def _encode_coil(value: object, data_type: str) -> bool:
    if data_type == "bool":
        if type(value) is not bool:
            raise TypeError("boolean coil requires bool value")
        return value

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("numeric coil requires 0 or 1")
    if value not in (0, 1):
        raise ValueError("numeric coil requires 0 or 1")
    return bool(value)


def _strict_integer_value(
    value: object,
    data_type: str,
    value_range: tuple[int, int],
) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{data_type} requires a numeric integer value")
    integer = int(value)
    if float(value) != float(integer):
        raise ValueError(f"{data_type} requires an integer value")
    lower, upper = value_range
    if not lower <= integer <= upper:
        raise ValueError(f"{data_type} value {integer} outside range {lower}..{upper}")
    return integer


def _strict_float_value(value: object, data_type: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{data_type} requires numeric value")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{data_type} requires a finite value")
    return number


def _strict_register(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Modbus register response must be integer")
    if not 0 <= value <= 0xFFFF:
        raise ValueError("Modbus register outside 0..65535")
    return value


def _as_point_scalar(value: object) -> float | int | bool | str | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported Modbus point value type '{type(value).__name__}'")
