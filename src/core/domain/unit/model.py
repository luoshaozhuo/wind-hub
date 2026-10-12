"""工程单位值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .quantity import Quantity


@dataclass(frozen=True, slots=True)
class Unit:
    """不可变工程单位值对象。

    scale_to_base 与 offset_to_base 将单位值映射到所属 Quantity 的基准单位：

    base_value = value * scale_to_base + offset_to_base

    unit_id 提供稳定语义身份（配置 units.yaml 的 Mapping Key），symbol
    提供工程显示符号；基准单位由 UnitCatalog 按 Quantity 集中维护，
    Unit 自身不重复声明。
    """

    unit_id: str
    symbol: str
    quantity: Quantity
    scale_to_base: float = 1.0
    offset_to_base: float = 0.0

    def __post_init__(self) -> None:
        unit_id = self.unit_id.strip()
        if not unit_id or unit_id != self.unit_id:
            raise ValueError("unit_id must be a non-empty trimmed string")
        if not isinstance(self.symbol, str):
            raise ValueError("symbol must be a string")
        if not isfinite(self.scale_to_base) or self.scale_to_base <= 0.0:
            raise ValueError("scale_to_base must be finite and greater than zero")
        if not isfinite(self.offset_to_base):
            raise ValueError("offset_to_base must be finite")

    @property
    def is_base_transform(self) -> bool:
        """是否相对基准单位保持恒等变换（scale=1 且 offset=0）。"""
        return self.scale_to_base == 1.0 and self.offset_to_base == 0.0
