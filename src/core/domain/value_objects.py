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


class DataType(StrEnum):
    """业务点对内、对外统一使用的标准标量类型。"""

    BOOL = "bool"
    INT8 = "int8"
    UINT8 = "uint8"
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    INT64 = "int64"
    UINT64 = "uint64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "str"


# 整数数据类型的取值范围；Domain 校验与协议 Driver 写值编码共用同一来源。
INTEGER_RANGES: dict[DataType, tuple[int, int]] = {
    DataType.INT8: (-128, 127),
    DataType.UINT8: (0, 255),
    DataType.INT16: (-32768, 32767),
    DataType.UINT16: (0, 65535),
    DataType.INT32: (-2147483648, 2147483647),
    DataType.UINT32: (0, 4294967295),
    DataType.INT64: (-9223372036854775808, 9223372036854775807),
    DataType.UINT64: (0, 18446744073709551615),
}
