"""Shared Domain 值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Protocol:
    """设备对外通信所采用的协议标识。"""

    name: str

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not name:
            raise ValueError("protocol name must not be empty")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class RawDataType:
    """协议点原始值类型。"""

    name: str

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not name:
            raise ValueError("raw data type must not be empty")
        object.__setattr__(self, "name", name)


class PointAccess(StrEnum):
    """设备协议点的读写能力。"""

    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


class ValueType(StrEnum):
    """业务点标准值类型。"""

    FLOAT = "float"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    STRING = "string"
