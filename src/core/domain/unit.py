"""Shared Domain 工程单位参考实体。"""

from __future__ import annotations

from dataclasses import dataclass

from .identities import UnitId


@dataclass(frozen=True, slots=True)
class Unit:
    """系统内稳定引用的工程单位。"""

    unit_id: UnitId
    symbol: str
    name: str | None = None

    def __post_init__(self) -> None:
        unit_id = self.unit_id.strip().lower()
        symbol = self.symbol.strip()
        name = self.name.strip() if self.name is not None else None

        if not unit_id:
            raise ValueError("unit_id must not be empty")
        if name == "":
            name = None

        object.__setattr__(self, "unit_id", UnitId(unit_id))
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "name", name)
