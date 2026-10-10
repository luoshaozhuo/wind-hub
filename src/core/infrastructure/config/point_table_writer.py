"""从完整 PointTable 领域对象还原继承式 points.yaml。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.domain import Point, PointTable, PointTableId


def _point_dict(point: Point) -> dict[str, Any]:
    address = dict(point.ext)
    data_type = address.pop("data_type")
    return {
        "business_point_id": str(point.business_point_id),
        "variable_name": point.variable_name,
        "point_groups": list(point.point_groups),
        "address": address,
        "data_type": data_type,
        "scale": point.scale,
        "offset": point.offset,
        "unit": point.source_unit.code.value,
        "description": point.description,
    }


def dump_point_tables(
    tables: Mapping[PointTableId, PointTable],
) -> dict[str, Any]:
    """以完整子表与父表的字段差异输出 extends / remove_points / points。

    生成语义等价的精简 YAML，不保留原注释及字段书写习惯。
    """
    result: dict[str, Any] = {}
    for table_id, table in tables.items():
        parent = tables[table.parent_id] if table.parent_id is not None else None
        definition: dict[str, Any] = {}
        if parent is None:
            definition["protocol"] = table.protocol.name
        else:
            definition["extends"] = str(table.parent_id)
            removed = [point_id for point_id in parent.points if point_id not in table.points]
            if removed:
                definition["remove_points"] = removed

        points: list[dict[str, Any]] = []
        for point_id, point in table.points.items():
            current = _point_dict(point)
            if parent is not None and point_id in parent.points:
                previous = _point_dict(parent.points[point_id])
                changes = {
                    key: value for key, value in current.items()
                    if value != previous[key]
                }
                if not changes:
                    continue
                points.append({"point_id": point_id, **changes})
            else:
                points.append({"point_id": point_id, **current})
        if points or parent is None:
            definition["points"] = points
        result[str(table_id)] = definition
    return {"point_tables": result}
