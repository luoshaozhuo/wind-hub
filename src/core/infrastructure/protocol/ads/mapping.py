"""ADS Point 地址与类型映射。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from core.application.protocol_contract import PointScalar

from core.application.errors import ConfigError
from core.domain import Point

from .codec import compile_decoder

_ALLOWED_OPTIONS = frozenset(
    {
        "symbol",
        "index_group",
        "index_offset",
        "data_type",
        "type",
        "size",
    }
)

_RAW_TO_ADS: dict[str, str] = {
    "bool": "BOOL",
    "int8": "SINT",
    "uint8": "USINT",
    "int16": "INT",
    "uint16": "UINT",
    "int32": "DINT",
    "uint32": "UDINT",
    "int64": "LINT",
    "uint64": "ULINT",
    "float32": "REAL",
    "float64": "LREAL",
    "str": "STRING",
}
_ADS_TYPE_SIZES: dict[str, int] = {
    "BOOL": 1,
    "SINT": 1,
    "USINT": 1,
    "INT": 2,
    "UINT": 2,
    "DINT": 4,
    "UDINT": 4,
    "LINT": 8,
    "ULINT": 8,
    "REAL": 4,
    "LREAL": 8,
    "STRING": 0,
}


@dataclass(frozen=True, slots=True)
class ADSPoint:
    """规范化 ADS 点地址。

    symbol 存在时，index 地址必须在当前 ADS session 建立后重新解析；
    未解析地址以 None 表示，不使用合法数值作占位。
    """

    point_id: str
    index_group: int | None
    index_offset: int | None
    data_type: str
    size: int
    symbol: str
    decoder: Callable[[bytes | memoryview, int], PointScalar] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        has_group = self.index_group is not None
        has_offset = self.index_offset is not None
        if has_group != has_offset:
            raise ConfigError(f"ADS point '{self.point_id}' requires a complete index address")
        if not self.symbol.strip():
            raise ConfigError(f"ADS point '{self.point_id}' requires a non-empty symbol")
        object.__setattr__(self, "decoder", compile_decoder(self))

    @property
    def address_resolved(self) -> bool:
        return self.index_group is not None and self.index_offset is not None

    @property
    def group(self) -> int:
        if self.index_group is None:
            raise ConfigError(f"ADS point '{self.point_id}' index address is unresolved")
        return self.index_group

    @property
    def offset(self) -> int:
        if self.index_offset is None:
            raise ConfigError(f"ADS point '{self.point_id}' index address is unresolved")
        return self.index_offset

    def resolved(
        self,
        *,
        index_group: int,
        index_offset: int,
        size: int | None = None,
    ) -> ADSPoint:
        """返回绑定当前 ADS session 地址的新值对象。"""
        return replace(
            self,
            index_group=index_group,
            index_offset=index_offset,
            size=self.size if size is None else size,
        )

    def unresolved(self) -> ADSPoint:
        """使 symbol 派生地址失效。"""
        return replace(self, index_group=None, index_offset=None)


undefined