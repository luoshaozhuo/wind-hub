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


def decode(point: ModbusPoint, values: Sequence[object]) -> bool | int | float:
    """统一解码一个配置测点。

    Coil/Discrete Input 解码为 bool；Holding/Input Register 按 data_type
    解码为数值。此处验证原始值类型及数量，避免截断报文被当作有效测点。
    """
    if len(values) != point.count:
        raise ValueError("Modbus point payload length mismatch")

    if point.register_type in _BIT_TYPES:
        bit = values[0]
        if type(bit) not in (bool, int) or bit not in (0, 1):
            raise ValueError("invalid Modbus bit value")
        return bool(bit)

    registers = [_strict_register(value) for value in values]
    if point.data_type == "int16":
        first = registers[0]
        return first if first < 0x8000 else first - 0x10000
    if point.data_type == "uint16":
        return registers[0]
    if point.data_type not in _MULTI_REGISTER_TYPES:
        raise ValueError(f"unsupported Modbus data type '{point.data_type}'")

    datatype = getattr(
        pymodbus.client.ModbusTcpClient.DATATYPE, point.data_type.upper(), None
    )
    if datatype is None:
        raise ValueError(f"unsupported PyModbus data type '{point.data_type}'")
    value = pymodbus.client.ModbusTcpClient.convert_from_registers(
        registers,
        datatype,
        word_order="little" if point.word_order == "little_endian" else "big",
    )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("unexpected Modbus numeric decode result")
    return value


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
    if isinstance(value, float) and (not isfinite(value) or not value.is_integer()):
        raise ValueError(f"{data_type} requires an integer value")
    integer = int(value)
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
