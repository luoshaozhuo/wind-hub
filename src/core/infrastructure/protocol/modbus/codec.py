"""Modbus 数值编解码；不持有网络资源。"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite

import pymodbus.client

from .mapping import ModbusPoint

_BIT_TYPES = frozenset({"coil", "discrete_input"})
_MULTI_REGISTER_TYPES = frozenset({"int32", "uint32", "float32", "int64", "uint64", "float64"})
_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "int16": (-32768, 32767),
    "uint16": (0, 65535),
    "int32": (-2147483648, 2147483647),
    "uint32": (0, 4294967295),
    "int64": (-9223372036854775808, 9223372036854775807),
    "uint64": (0, 18446744073709551615),
}

def _decode_point(
    point: ModbusPoint,
    segment: Sequence[object],
) -> object:
    if not segment:
        raise ValueError("empty Modbus response segment")

    if point.register_type in _BIT_TYPES:
        return bool(segment[0])

    registers = [_strict_register(value) for value in segment]
    return _decode_registers(registers, point.data_type, point.word_order)


def _decode_registers(
    registers: list[int],
    data_type: str,
    word_order: str,
) -> object:
    first = registers[0]
    if data_type == "int16":
        return first if first < 0x8000 else first - 0x10000
    if data_type == "uint16":
        return first

    if data_type not in _MULTI_REGISTER_TYPES:
        raise ValueError(f"unsupported Modbus data type '{data_type}'")

    # 通用 32/64 位编解码交由 PyModbus，避免自行拼接二进制字节流。
    tcp_client = pymodbus.client.ModbusTcpClient
    datatype = getattr(tcp_client.DATATYPE, data_type.upper(), None)
    if datatype is None:
        raise ValueError(f"unsupported PyModbus data type '{data_type}'")
    return tcp_client.convert_from_registers(
        registers,
        datatype,
        word_order="little" if word_order == "little_endian" else "big",
    )


def _encode_registers(
    value: object,
    data_type: str,
    word_order: str,
) -> list[int]:
    integer_range = _INTEGER_RANGES.get(data_type)
    if integer_range is not None:
        number: int | float = _strict_integer_value(value, data_type, integer_range)
        if data_type in {"int16", "uint16"}:
            return [int(number) & 0xFFFF]
    elif data_type in {"float32", "float64"}:
        number = _strict_float_value(value, data_type)
    else:
        raise ValueError(f"unsupported Modbus data type '{data_type}'")

    tcp_client = pymodbus.client.ModbusTcpClient
    datatype = getattr(tcp_client.DATATYPE, data_type.upper(), None)
    if datatype is None:
        raise ValueError(f"unsupported PyModbus data type '{data_type}'")
    words: list[int] = tcp_client.convert_to_registers(
        number,
        datatype,
        word_order="little" if word_order == "little_endian" else "big",
    )
    return words


def _encode_coil(value: object, data_type: str) -> bool:
    if data_type != "bool" or type(value) is not bool:
        raise TypeError("Modbus Coil requires bool value")
    return value


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
