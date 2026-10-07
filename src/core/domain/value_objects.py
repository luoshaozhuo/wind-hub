"""Shared Domain 值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


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

    def coerce(self, value: object) -> bool | int | float | str:
        """把值严格收敛到业务点声明的标准类型。"""
        if self is DataType.BOOL:
            if type(value) is not bool:
                raise TypeError("bool data type requires bool value")
            return value

        if self is DataType.STRING:
            if not isinstance(value, str):
                raise TypeError("string data type requires str value")
            return value

        integer_range = _INTEGER_RANGES.get(self)
        if integer_range is not None:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(f"{self.value} data type requires numeric value")
            integer = int(value)
            if float(value) != float(integer):
                raise ValueError(f"{self.value} data type requires integer value")
            lower, upper = integer_range
            if not lower <= integer <= upper:
                raise ValueError(
                    f"{self.value} value {integer} outside range {lower}..{upper}"
                )
            return integer

        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"{self.value} data type requires numeric value")
        number = float(value)
        if not isfinite(number):
            raise ValueError(f"{self.value} data type requires finite value")
        return number


_INTEGER_RANGES: dict[DataType, tuple[int, int]] = {
    DataType.INT8: (-128, 127),
    DataType.UINT8: (0, 255),
    DataType.INT16: (-32768, 32767),
    DataType.UINT16: (0, 65535),
    DataType.INT32: (-2147483648, 2147483647),
    DataType.UINT32: (0, 4294967295),
    DataType.INT64: (-9223372036854775808, 9223372036854775807),
    DataType.UINT64: (0, 18446744073709551615),
}
