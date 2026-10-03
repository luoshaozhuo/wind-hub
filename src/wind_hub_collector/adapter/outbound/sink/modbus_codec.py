"""Modbus Sink 值编码。

只负责把已经完成 SinkReferenceExporter 变换的外部点值编码为 Modbus bit
或 16-bit register；不创建 datastore、不启动 TCP Server。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from wind_hub_collector.application.sink_export import ExportedSinkPointValue
from wind_hub_core.config.sinks import ModbusSinkAddress


MODBUS_WORD_WIDTH: dict[str, int] = {
    "bool": 1,
    "int8": 1,
    "uint8": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
}

_STRUCT_FORMAT: dict[str, str] = {
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "float32": "f",
    "int64": "q",
    "uint64": "Q",
    "float64": "d",
}


@dataclass(frozen=True, slots=True)
class EncodedModbusValue:
    """一个已编码的 Modbus Sink 点值。"""

    unit_id: int
    register_type: str
    address: int
    bits: tuple[bool, ...] = ()
    registers: tuple[int, ...] = ()


def encode_modbus_value(value: ExportedSinkPointValue) -> EncodedModbusValue:
    """按 ResolvedSinkPoint 的 datatype/byte_order/word_order 编码值。"""
    definition = value.definition
    address = definition.address
    if not isinstance(address, ModbusSinkAddress):
        raise TypeError(
            f"Sink point '{definition.ref}' does not use ModbusSinkAddress"
        )
    if value.value is None:
        raise ValueError(f"Sink point '{definition.ref}' value is None")

    if address.register_type in {"coil", "discrete"}:
        if not isinstance(value.value, bool):
            raise TypeError(
                f"Sink point '{definition.ref}' requires bool value for "
                f"{address.register_type}"
            )
        return EncodedModbusValue(
            unit_id=address.unit_id,
            register_type=address.register_type,
            address=address.address,
            bits=(value.value,),
        )

    registers = _encode_registers(
        value.value,
        definition.datatype,
        byte_order=address.byte_order,
        word_order=address.word_order,
    )
    return EncodedModbusValue(
        unit_id=address.unit_id,
        register_type=address.register_type,
        address=address.address,
        registers=tuple(registers),
    )


def _encode_registers(
    value: object,
    datatype: str,
    *,
    byte_order: str,
    word_order: str,
) -> list[int]:
    """编码一个 word-addressed Modbus 值。"""
    if datatype == "bool":
        if not isinstance(value, bool):
            raise TypeError("bool datatype requires bool value")
        return [1 if value else 0]
    if datatype == "int8":
        if isinstance(value, bool):
            raise TypeError("int8 datatype requires integer value")
        return [struct.unpack(">B", struct.pack(">b", int(value)))[0]]
    if datatype == "uint8":
        if isinstance(value, bool):
            raise TypeError("uint8 datatype requires integer value")
        return [struct.unpack(">B", struct.pack(">B", int(value)))[0]]

    fmt = _STRUCT_FORMAT.get(datatype)
    if fmt is None:
        raise TypeError(f"Unsupported Modbus datatype '{datatype}'")

    if datatype.startswith("float"):
        scalar: int | float = float(value)  # type: ignore[arg-type]
    else:
        if isinstance(value, bool):
            raise TypeError(f"{datatype} datatype requires integer value")
        scalar = int(value)  # type: ignore[arg-type]

    raw = struct.pack(">" + fmt, scalar)
    words = [raw[i : i + 2] for i in range(0, len(raw), 2)]
    if byte_order == "little":
        words = [word[::-1] for word in words]
    if word_order == "little":
        words.reverse()
    return [int.from_bytes(word, byteorder="big") for word in words]


__all__ = ["MODBUS_WORD_WIDTH", "EncodedModbusValue", "encode_modbus_value"]
