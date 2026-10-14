"""Shared Domain 工程单位。

Unit / Quantity 是不可变值对象；单位集合由配置（units.yaml）定义并
经 UnitCatalog 随 ConfigSnapshot 传递，不在代码中枚举。

UnitCatalog 由配置（units.yaml）构造并随 ConfigSnapshot 传递，是不可变
值对象而非全局可变 Singleton；Domain 不读取文件，目录完整性在构建时
一次校验完成。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class Quantity:
    """工程量身份值对象。

    这里表达业务量类别，而不是纯物理量纲。例如有功、无功和视在功率
    具有相同物理量纲，但在业务语义上不可互换，因此分别建模。

    具体 Quantity 集合由配置（units.yaml）定义；Domain 只固定身份
    语义（字符串 ID 值对象），不枚举全部工程量。
    """

    quantity_id: str

    def __post_init__(self) -> None:
        quantity_id = self.quantity_id.strip()
        if not quantity_id or quantity_id != self.quantity_id:
            raise ValueError("quantity_id must be a non-empty trimmed string")

    def __str__(self) -> str:
        return self.quantity_id


#: 无量纲是 Domain 校验固有语义（bool/string 点必须无量纲），
#: 与配置定义的具体 Quantity 集合无关，因此保留为代码常量。
DIMENSIONLESS = Quantity("dimensionless")


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


def convert_value(value: float, source: Unit, target: Unit) -> float:
    """在同一工程量类别的单位之间换算数值。

    Args:
        value: 源单位下的数值。
        source: 源单位。
        target: 目标单位。

    Returns:
        目标单位下的数值。

    Raises:
        ValueError: source 与 target 属于不同 Quantity。
    """
    if source.quantity != target.quantity:
        raise ValueError(f"incompatible unit quantities: {source.quantity} -> {target.quantity}")

    base_value = value * source.scale_to_base + source.offset_to_base
    return (base_value - target.offset_to_base) / target.scale_to_base


@dataclass(frozen=True, slots=True)
class UnitCatalog:
    """一次配置加载内的完整工程单位目录。

    Attributes:
        units: ``{unit_id: Unit}`` 单位索引。
        base_units: ``{quantity_id: base_unit_id}`` 每个 Quantity 唯一的
            基准单位声明。
    """

    units: Mapping[str, Unit]
    base_units: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", MappingProxyType(dict(self.units)))
        object.__setattr__(self, "base_units", MappingProxyType(dict(self.base_units)))

    @classmethod
    def build(
        cls,
        *,
        quantities: Mapping[str, str],
        units: Iterable[Unit],
    ) -> UnitCatalog:
        """构建并完整校验单位目录。

        Args:
            quantities: ``{quantity_id: base_unit_id}``。
            units: 全部 Unit；``Unit.quantity`` 必须引用已声明的 Quantity。

        Raises:
            ValueError: 目录不完整或自相矛盾（未定义引用、基准单位不属于
                对应 Quantity、基准单位非恒等变换、重复单位身份等）。
        """
        base_units: dict[str, str] = {}
        for quantity_id, base_unit_id in quantities.items():
            if not isinstance(quantity_id, str) or not quantity_id.strip():
                raise ValueError("quantity IDs must be non-empty trimmed strings")
            if quantity_id != quantity_id.strip():
                raise ValueError(f"quantity ID '{quantity_id}' has surrounding whitespace")
            if not isinstance(base_unit_id, str) or not base_unit_id.strip():
                raise ValueError(f"quantity '{quantity_id}' base_unit must be a non-empty string")
            if quantity_id in base_units:
                raise ValueError(f"duplicate quantity ID '{quantity_id}'")
            base_units[quantity_id] = base_unit_id.strip()

        index: dict[str, Unit] = {}
        for unit in units:
            if unit.unit_id in index:
                raise ValueError(f"duplicate unit ID '{unit.unit_id}'")
            if unit.quantity.quantity_id not in base_units:
                raise ValueError(
                    f"unit '{unit.unit_id}' references undefined quantity "
                    f"'{unit.quantity.quantity_id}'"
                )
            index[unit.unit_id] = unit

        for quantity_id, base_unit_id in base_units.items():
            base = index.get(base_unit_id)
            if base is None:
                raise ValueError(
                    f"quantity '{quantity_id}' base_unit '{base_unit_id}' is not defined"
                )
            if base.quantity.quantity_id != quantity_id:
                raise ValueError(
                    f"quantity '{quantity_id}' base_unit '{base_unit_id}' belongs to "
                    f"quantity '{base.quantity.quantity_id}'"
                )
            if not base.is_base_transform:
                raise ValueError(
                    f"base unit '{base_unit_id}' of quantity '{quantity_id}' must use "
                    "scale_to_base=1 and offset_to_base=0"
                )
        return cls(units=index, base_units=base_units)

    def __contains__(self, unit_id: object) -> bool:
        return unit_id in self.units

    def unit(self, unit_id: str) -> Unit:
        """按 Unit ID 返回单位；未定义时抛出 ValueError。"""
        try:
            return self.units[unit_id]
        except KeyError:
            raise ValueError(f"undefined unit ID '{unit_id}'") from None

    def quantity_ids(self) -> frozenset[str]:
        """返回目录中全部 Quantity ID。"""
        return frozenset(self.base_units)

    def base_unit(self, quantity_id: str) -> Unit:
        """返回指定 Quantity 的基准单位。"""
        base_unit_id = self.base_units.get(quantity_id)
        if base_unit_id is None:
            raise ValueError(f"undefined quantity ID '{quantity_id}'")
        return self.units[base_unit_id]

    def convert(self, value: float, source_id: str, target_id: str) -> float:
        """在同一 Quantity 内按 Unit ID 换算数值。"""
        return convert_value(value, self.unit(source_id), self.unit(target_id))


__all__ = [
    "DIMENSIONLESS",
    "Quantity",
    "Unit",
    "UnitCatalog",
    "convert_value",
]
