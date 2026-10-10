"""业务点标准目录 YAML 转换（可选文件，显式声明优先）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.errors import ConfigError
from core.domain import BusinessPoint, BusinessPointId, DataType
from core.domain.unit import UNIT_CATALOG, UnitCode


def parse_business_points(raw: Mapping[str, Any]) -> dict[BusinessPointId, BusinessPoint]:
    """按 ID 建立全场共享的业务点目录。"""
    if set(raw) != {"business_points"}:
        raise ConfigError("business_points.yaml requires only 'business_points' root")
    entries = raw["business_points"]
    if not isinstance(entries, dict):
        raise ConfigError("business_points must be a mapping keyed by business point ID")
    points: dict[BusinessPointId, BusinessPoint] = {}
    for identity, value in entries.items():
        if not isinstance(identity, str) or not identity.strip() or identity != identity.strip():
            raise ConfigError("business point IDs must be non-empty trimmed strings")
        if not isinstance(value, dict) or set(value) - {"data_type", "unit", "description"}:
            raise ConfigError(f"invalid business point '{identity}' fields")
        try:
            data_type = DataType(value["data_type"])
            unit = UNIT_CATALOG[UnitCode(value["unit"])]
            description = value.get("description")
            if description is not None and not isinstance(description, str):
                raise ValueError("description must be string")
            points[BusinessPointId(identity)] = BusinessPoint(
                business_point_id=BusinessPointId(identity),
                data_type=data_type,
                standard_unit=unit,
                description=description,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"invalid business point '{identity}': {exc}") from exc
    return points
