"""只读工程单位目录。

UnitCatalog 由配置（units.yaml）构造并随 ConfigSnapshot 传递，是不可变
值对象而非全局可变 Singleton；Domain 不读取文件，目录完整性在构建时
一次校验完成。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .conversion import convert_value
from .model import Unit


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


__all__ = ["UnitCatalog"]
