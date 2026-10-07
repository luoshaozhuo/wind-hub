"""基于 pymodbus 的共享 Modbus TCP ProtocolPort Adapter。"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Sequence
from math import isfinite
from typing import Any

from core.application.config import (
    DeviceConnection,
    PointProtocolOptions,
    PointTable,
    ProtocolOptions,
    ProtocolPoint,
)
from core.application.errors import ConfigError, ProtocolError
from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)

from .config import ModbusConfig, parse_modbus_config
from .mapping import ModbusPoint, group_consecutive_reads, parse_modbus_point

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


class ModbusDriver:
    """单个 DeviceConnection 的 Modbus TCP Driver。

    Driver 只执行单次 connect/read/write，不负责重连退避或采集调度。一个实例
    由单一 asyncio event loop 持有，内部 Lock 防止同一 client 上读写交错。
    """

    def __init__(
        self,
        connection: DeviceConnection,
        point_table: PointTable,
    ) -> None:
        if point_table.protocol.name != "modbus":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'modbus'"
            )

        self._connection = connection
        self._config: ModbusConfig = parse_modbus_config(connection)
        self._points = {
            point.point_id: parse_modbus_point(
                point,
                default_word_order=self._config.word_order,
            )
            for point in point_table.points.values()
        }

        self._lock = asyncio.Lock()
        self._client: Any = None
        self._connected = False

    async def connect(self) -> None:
        """建立一次 Modbus TCP 连接；不在 Driver 内部重试。"""
        async with self._lock:
            if self._connected:
                return

            try:
                from pymodbus.client import AsyncModbusTcpClient
            except ImportError as exc:
                raise ProtocolError(
                    "Modbus support requires the optional 'pymodbus' dependency"
                ) from exc

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
                    f"Modbus connect failed for {self._config.host}:"
                    f"{self._config.port}: {exc}"
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

    async def read(
        self,
        points: Sequence[ProtocolPoint],
    ) -> tuple[ProtocolSample, ...]:
        """批量读取协议点，并合并相邻 Modbus 地址。"""
        if not points:
            return ()

        async with self._lock:
            if not self._connected:
                raise ProtocolError("Modbus read requires an active connection")

            mapped = [self._mapped_point(point) for point in points]
            try:
                values: dict[str, object] = {}
                for group in group_consecutive_reads(mapped):
                    values.update(await self._read_group(group))
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"Modbus read failed: {exc}") from exc

            samples: list[ProtocolSample] = []
            for point in points:
                value = values.get(point.point_id, _DECODE_FAILED)
                if value is _DECODE_FAILED:
                    samples.append(
                        ProtocolSample(
                            point_id=point.point_id,
                            value=None,
                            quality=Quality.BAD,
                        )
                    )
                else:
                    samples.append(
                        ProtocolSample(
                            point_id=point.point_id,
                            value=_as_point_scalar(value),
                            quality=Quality.GOOD,
                        )
                    )
            return tuple(samples)

    async def write(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """逐点执行写入；单点配置/设备拒绝不会取消同批其它点。"""
        if not writes:
            return ()

        async with self._lock:
            if not self._connected:
                raise ProtocolError("Modbus write requires an active connection")

            results: list[ProtocolWriteResult] = []
            try:
                for write in writes:
                    mapped = self._mapped_point(write.point)
                    if mapped.register_type in _READ_ONLY_TYPES:
                        results.append(
                            ProtocolWriteResult(
                                point_id=mapped.point_id,
                                success=False,
                                message=f"{mapped.register_type} is read-only",
                            )
                        )
                        continue
                    try:
                        accepted = await self._write_single(mapped, write.value)
                    except (TypeError, ValueError, struct.error) as exc:
                        results.append(
                            ProtocolWriteResult(
                                point_id=mapped.point_id,
                                success=False,
                                message=str(exc) or type(exc).__name__,
                            )
                        )
                        continue
                    if accepted:
                        results.append(
                            ProtocolWriteResult(
                                point_id=mapped.point_id,
                                success=True,
                            )
                        )
                    else:
                        results.append(
                            ProtocolWriteResult(
                                point_id=mapped.point_id,
                                success=False,
                                message="Modbus exception response",
                            )
                        )
            except ProtocolError:
                raise
            except Exception as exc:
                self._signal_disconnect()
                raise ProtocolError(f"Modbus write failed: {exc}") from exc

            return tuple(results)

    def _mapped_point(self, point: ProtocolPoint) -> ModbusPoint:
        mapped = self._points.get(point.point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point.point_id}' is not part of connection "
                f"'{self._connection.connection_id}' point table"
            )
        return mapped

    async def _read_group(
        self,
        group: list[ModbusPoint],
    ) -> dict[str, object]:
        client = self._client
        register_type = group[0].register_type
        start = min(point.address for point in group)
        end = max(point.address + point.count for point in group)
        count = end - start
        unit_id = self._config.unit_id

        if register_type == "coil":
            response = await client.read_coils(
                start,
                count=count,
                device_id=unit_id,
            )
        elif register_type == "discrete_input":
            response = await client.read_discrete_inputs(
                start,
                count=count,
                device_id=unit_id,
            )
        elif register_type == "holding":
            response = await client.read_holding_registers(
                start,
                count=count,
                device_id=unit_id,
            )
        else:
            response = await client.read_input_registers(
                start,
                count=count,
                device_id=unit_id,
            )

        if response.isError():
            raise ProtocolError(
                f"Modbus {register_type} read at {start} count={count} "
                "returned an exception response"
            )

        raw: list[object]
        if register_type in _BIT_TYPES:
            raw = list(response.bits)
        else:
            raw = list(response.registers)

        values: dict[str, object] = {}
        for point in group:
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
    segment: list[object],
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

    words = list(registers)
    if word_order == "little_endian":
        words.reverse()
    raw = b"".join(struct.pack(">H", word) for word in words)
    return struct.unpack(fmt, raw)[0]


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
        integer = _strict_integer_value(value, data_type, integer_range)
        if data_type in {"int8", "uint8", "int16", "uint16"}:
            return [integer & 0xFFFF]
        fmt = _MULTI_REGISTER_FMT[data_type]
        raw = struct.pack(fmt, integer)
    elif data_type in {"float32", "float64"}:
        number = _strict_float_value(value, data_type)
        raw = struct.pack(_MULTI_REGISTER_FMT[data_type], number)
    else:
        raise ValueError(f"unsupported Modbus data type '{data_type}'")

    words = list(struct.unpack(">" + "H" * (len(raw) // 2), raw))
    if word_order == "little_endian":
        words.reverse()
    return words


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
        raise ValueError(
            f"{data_type} value {integer} outside range {lower}..{upper}"
        )
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
