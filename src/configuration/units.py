"""units.yaml 与 UnitCatalog 的双向转换。

Domain 不依赖 YAML；本模块负责把 ``quantities`` / ``units`` 两段配置
解析为 :class:`UnitCatalog`，并把目录无损写回 YAML 文档结构。
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from core.application.errors import ConfigError
from core.domain.unit import Quantity, Unit, UnitCatalog

_QUANTITY_FIELDS = {"base_unit"}
_UNIT_FIELDS = {"symbol", "quantity", "scale_to_base", "offset_to_base"}


def _id_key(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigError(f"{context}: IDs must be non-empty trimmed strings")
    return value


def _number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ConfigError(f"{context} must be a finite number")
    return float(value)


def parse_unit_catalog(raw: Mapping[str, Any]) -> UnitCatalog:
    """解析 units.yaml 根映射为 UnitCatalog 并完成目录级校验。"""
    extra = set(raw) - {"quantities", "units"}
    if extra:
        raise ConfigError(f"units.yaml unknown keys: {sorted(extra)}")
    raw_quantities = raw.get("quantities")
    if not isinstance(raw_quantities, dict) or not raw_quantities:
        raise ConfigError("units.yaml requires a non-empty 'quantities' mapping")
    quantities: dict[str, str] = {}
    for raw_key, item in raw_quantities.items():
        quantity_id = _id_key(raw_key, "units.yaml quantities")
        context = f"quantity '{quantity_id}'"
        if not isinstance(item, dict):
            raise ConfigError(f"units.yaml {context} must be a mapping")
        unknown = set(item) - _QUANTITY_FIELDS
        if unknown:
            raise ConfigError(f"units.yaml {context} unknown keys: {sorted(unknown)}")
        base_unit = item.get("base_unit")
        if not isinstance(base_unit, str) or not base_unit.strip():
            raise ConfigError(f"units.yaml {context}.base_unit must be a non-empty string")
        quantities[quantity_id] = base_unit.strip()

    raw_units = raw.get("units")
    if not isinstance(raw_units, dict) or not raw_units:
        raise ConfigError("units.yaml requires a non-empty 'units' mapping")
    units: list[Unit] = []
    for raw_key, item in raw_units.items():
        unit_id = _id_key(raw_key, "units.yaml units")
        context = f"unit '{unit_id}'"
        if not isinstance(item, dict):
            raise ConfigError(f"units.yaml {context} must be a mapping")
        unknown = set(item) - _UNIT_FIELDS
        if unknown:
            raise ConfigError(f"units.yaml {context} unknown keys: {sorted(unknown)}")
        symbol = item.get("symbol")
        if not isinstance(symbol, str):
            raise ConfigError(f"units.yaml {context}.symbol must be a string")
        quantity_ref = item.get("quantity")
        if not isinstance(quantity_ref, str) or not quantity_ref.strip():
            raise ConfigError(f"units.yaml {context}.quantity must be a non-empty string")
        scale = _number(item.get("scale_to_base", 1.0), f"units.yaml {context}.scale_to_base")
        offset = _number(item.get("offset_to_base", 0.0), f"units.yaml {context}.offset_to_base")
        try:
            units.append(
                Unit(
                    unit_id=unit_id,
                    symbol=symbol,
                    quantity=Quantity(quantity_ref.strip()),
                    scale_to_base=scale,
                    offset_to_base=offset,
                )
            )
        except ValueError as exc:
            raise ConfigError(f"units.yaml {context}: {exc}") from exc
    try:
        return UnitCatalog.build(quantities=quantities, units=units)
    except ValueError as exc:
        raise ConfigError(f"units.yaml: {exc}") from exc


def dump_unit_catalog(catalog: UnitCatalog) -> dict[str, Any]:
    """把 UnitCatalog 序列化为 units.yaml 文档结构（往返无损）。"""
    quantities = {
        quantity_id: {"base_unit": base_unit_id}
        for quantity_id, base_unit_id in catalog.base_units.items()
    }
    units: dict[str, Any] = {}
    for unit_id, unit in catalog.units.items():
        entry: dict[str, Any] = {
            "symbol": unit.symbol,
            "quantity": unit.quantity.quantity_id,
        }
        if unit.scale_to_base != 1.0:
            entry["scale_to_base"] = unit.scale_to_base
        if unit.offset_to_base != 0.0:
            entry["offset_to_base"] = unit.offset_to_base
        units[unit_id] = entry
    return {"quantities": quantities, "units": units}


__all__ = ["dump_unit_catalog", "parse_unit_catalog"]
