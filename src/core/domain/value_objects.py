"""Shared Domain 值对象。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProtocolType:
    """协议类型。

    这里只表达领域层对协议的稳定识别，不包含 connect/read/write 等技术能力。
    """

    name: str

    def __post_init__(self) -> None:
        normalized = self.name.strip().lower()
        if not normalized:
            raise ValueError("protocol name must not be empty")
        object.__setattr__(self, "name", normalized)


@dataclass(frozen=True, slots=True)
class Unit:
    """工程单位。"""

    symbol: str
    name: str | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip()
        if not symbol:
            raise ValueError("unit symbol must not be empty")
        object.__setattr__(self, "symbol", symbol)
