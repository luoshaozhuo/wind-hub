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
class ConnectionEndpoint:
    """设备可达的逻辑通信端点。"""

    host: str
    port: int | None = None

    def __post_init__(self) -> None:
        host = self.host.strip()
        if not host:
            raise ValueError("endpoint host must not be empty")
        if self.port is not None:
            if isinstance(self.port, bool) or not isinstance(self.port, int):
                raise ValueError("endpoint port must be an integer or null")
            if not 1 <= self.port <= 65535:
                raise ValueError("endpoint port must be between 1 and 65535")
        object.__setattr__(self, "host", host)


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
