"""Modbus 地址映射与寄存器编码；核心 PointStore 不依赖 Modbus。"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Literal, cast

from .domain import Scalar

RegisterArea = Literal["coil", "discrete_input", "holding", "input"]
WordOrder = Literal["big_endian", "little_endian"]

_FORMATS = {
    "bool": "?",
    "int16": "h",
    "uint16": "H",
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "uint64": "Q",
    "float32": "f",
    "float64": "d",
}


@dataclass(frozen=True, slots=True)
class RegisterMapping:
    device_id: str
    point_id: str
    area: RegisterArea
    address: int
    data_type: str
    word_order: WordOrder = "big_endian"

    def __post_init__(self) -> None:
        if not self.device_id or not self.point_id:
            raise ValueError("device_id and point_id are required")
        if self.area not in {"coil", "discrete_input", "holding", "input"}:
            raise ValueError(f"unsupported register area: {self.area}")
        if self.data_type not in _FORMATS:
            raise ValueError(f"unsupported data type: {self.data_type}")
        if self.word_order not in {"big_endian", "little_endian"}:
            raise ValueError(f"unsupported word order: {self.word_order}")
        if self.area in {"coil", "discrete_input"} and self.data_type != "bool":
            raise ValueError("bit areas require bool")
        if type(self.address) is not int or not 0 <= self.address <= 65535:
            raise ValueError("address out of range")
        if self.address + self.count > 65536:
            raise ValueError("register span exceeds address space")

    @property
    def count(self) -> int:
        if self.area in {"coil", "discrete_input"}:
            return 1
        return max(1, struct.calcsize(">" + _FORMATS[self.data_type]) // 2)


def encode_registers(mapping: RegisterMapping, value: Scalar) -> tuple[int, ...]:
    """编码为网络字节序的 16 位寄存器序列。"""
    if mapping.data_type == "bool":
        if not isinstance(value, bool):
            raise TypeError("bool register requires bool")
        return (int(value),)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("numeric register requires numeric value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite register value")
    fmt = _FORMATS[mapping.data_type]
    if fmt in {"h", "H", "i", "I", "q", "Q"} and type(value) is not int:
        raise TypeError("integer register requires int")
    try:
        packed = struct.pack(">" + fmt, value)
    except (OverflowError, struct.error) as exc:
        raise ValueError(f"value out of range for {mapping.data_type}") from exc
    words = tuple(int.from_bytes(packed[i : i + 2], "big") for i in range(0, len(packed), 2))
    return words[::-1] if mapping.word_order == "little_endian" else words


def decode_registers(mapping: RegisterMapping, registers: list[int] | tuple[int, ...]) -> Scalar:
    """完整解码，拒绝半个多寄存器控制值。"""
    if len(registers) != mapping.count:
        raise ValueError("incomplete register value")
    if any(type(item) is not int or not 0 <= item <= 65535 for item in registers):
        raise ValueError("invalid register word")
    words = registers[::-1] if mapping.word_order == "little_endian" else registers
    if mapping.data_type == "bool":
        if words[0] not in (0, 1):
            raise ValueError("invalid boolean value")
        return bool(words[0])
    packed = b"".join(item.to_bytes(2, "big") for item in words)
    value = struct.unpack(">" + _FORMATS[mapping.data_type], packed)[0]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite register value")
    return cast(Scalar, value)


class RegisterMap:
    """阻止地址交叠，并要求一次写入完整覆盖一个逻辑测点。"""

    def __init__(self, mappings: list[RegisterMapping]) -> None:
        self._by_area: dict[RegisterArea, dict[int, RegisterMapping]] = {}
        for mapping in mappings:
            used = self._by_area.setdefault(mapping.area, {})
            for address in range(mapping.address, mapping.address + mapping.count):
                if address in used:
                    raise ValueError(f"overlapping register {mapping.area}:{address}")
                used[address] = mapping

    def resolve_write(self, area: RegisterArea, address: int, count: int) -> RegisterMapping:
        if area not in {"coil", "holding"}:
            raise PermissionError("register area is read only")
        mapping = self._by_area.get(area, {}).get(address)
        if mapping is None or mapping.address != address or mapping.count != count:
            raise ValueError("write must cover exactly one complete mapped point")
        return mapping
