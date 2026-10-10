"""Sink 点映射与 Modbus 地址冲突检查；纯数据语义，无 I/O。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal


RegisterType = Literal["holding", "input", "coil", "discrete"]
ModbusType = Literal[
    "bool", "int16", "uint16", "int32", "uint32", "int64", "uint64", "float32", "float64"
]

_REGISTER_WIDTH: dict[str, int] = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
}


@dataclass(frozen=True, slots=True)
class SinkSource:
    """点在采集域内的唯一引用。"""

    device_id: str
    point_id: str

    def __post_init__(self) -> None:
        if not self.device_id.strip() or not self.point_id.strip():
            raise ValueError("device_id and point_id must not be empty")


@dataclass(frozen=True, slots=True)
class ModbusAddress:
    """Modbus TCP Server 对外寄存器地址（零基）。"""

    unit_id: int
    register_type: RegisterType
    address: int
    data_type: ModbusType
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"

    def __post_init__(self) -> None:
        if not 0 <= self.unit_id <= 255:
            raise ValueError("unit_id must be in 0..255")
        if self.register_type not in ("holding", "input", "coil", "discrete"):
            raise ValueError("invalid register_type")
        if self.data_type not in _REGISTER_WIDTH:
            raise ValueError("invalid data_type")
        if self.register_type in ("coil", "discrete") and self.data_type != "bool":
            raise ValueError("bit tables only support bool")
        if not 0 <= self.address <= 65535:
            raise ValueError("address must be in 0..65535")
        if self.address + self.width > 65536:
            raise ValueError("address range exceeds Modbus table")
        if self.byte_order not in ("big", "little") or self.word_order not in ("big", "little"):
            raise ValueError("invalid byte or word order")

    @property
    def width(self) -> int:
        return _REGISTER_WIDTH[self.data_type]


@dataclass(frozen=True, slots=True)
class SinkPointMapping:
    """内部点到外部 Modbus 地址的映射与数值变换。"""

    source: SinkSource
    address: ModbusAddress
    scale: float = 1.0
    offset: float = 0.0

    def __post_init__(self) -> None:
        if not isfinite(self.scale) or self.scale == 0:
            raise ValueError("scale must be finite and non-zero")
        if not isfinite(self.offset):
            raise ValueError("offset must be finite")


def validate_modbus_mappings(mappings: list[SinkPointMapping]) -> None:
    """禁止同一 unit/table 寄存器区间交叠及重复的 source 映射。"""

    sources: set[SinkSource] = set()
    occupied: dict[tuple[int, str, int], SinkSource] = {}
    for item in mappings:
        if item.source in sources:
            raise ValueError(f"duplicate sink source: {item.source}")
        sources.add(item.source)
        address = item.address
        for register in range(address.address, address.address + address.width):
            key = (address.unit_id, address.register_type, register)
            if key in occupied:
                raise ValueError(
                    f"overlapping Modbus address {key}: {occupied[key]} and {item.source}"
                )
            occupied[key] = item.source
