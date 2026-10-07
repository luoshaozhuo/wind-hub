"""Shared Domain 值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Protocol:
    """协议标识。

    只表达领域层对协议的稳定识别，不包含连接、读写或订阅等技术能力。
    """

    name: str

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not name:
            raise ValueError("protocol name must not be empty")
        object.__setattr__(self, "name", name)


class ValueType(StrEnum):
    """业务点标准值类型。"""

    FLOAT = "float"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    STRING = "string"


@dataclass(frozen=True, slots=True)
class RawDataType:
    """协议侧原始数据类型。

    不在 Shared Domain 中维护协议类型白名单；具体协议负责解释其合法取值。
    """

    name: str

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not name:
            raise ValueError("raw data type must not be empty")
        object.__setattr__(self, "name", name)


class PointAccess(StrEnum):
    """协议点访问能力。"""

    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"
