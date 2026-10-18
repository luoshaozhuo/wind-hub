"""从完整 PointTable 领域对象还原继承式 points.yaml。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.domain import Point, PointTable, PointTableId


def _point_dict(point: Point) -> dict[str, Any]:
    address = dict(point.ext)
    data_type = address.pop("data_type")
    return {
        "point_groups": list(point.point_groups),
        **({"business_point_id": str(point.business_point_id)} if point.business_point_id != point.point_id else {}),
        "address": address,
        "data_type": data_type,
        "scale": point.scale,
        "offset": point.offset,
        "unit": point.source_unit.unit_id,
        "description": point.description,
    }


def dump_point_tables(
    tables: Mapping[PointTableId, PointTable],
) -> dict[str, Any]:
    """以完整子表与父表的字段差异输出 parent_id / remove_points / points。

    子表 ``points`` 是 Mapping（Key 即 Point ID），条目只携带相对父表
    变化的字段；加载端按「父表展开 + 条目合并」还原，与 Writer 使用同一
    套 Schema。生成语义等价的精简 YAML，不保留原注释及字段书写习惯。
    """
    result: dict[str, Any] = {}
    for table_id, table in tables.items():
        parent = tables[table.parent_id] if table.parent_id is not None else None
        definition: dict[str, Any] = {}
        if parent is None:
            definition["protocol"] = table.protocol.name
        else:
            definition["parent_id"] = str(table.parent_id)
            removed = [point_id for point_id in parent.points if point_id not in table.points]
            if removed:
                definition["remove_points"] = removed

        points: dict[str, Any] = {}
        for point_id, point in table.points.items():
            current = _point_dict(point)
            if parent is not None and point_id in parent.points:
                previous = _point_dict(parent.points[point_id])
                changes = {key: value for key, value in current.items() if value != previous.get(key)}
                if "business_point_id" in previous and "business_point_id" not in current:
                    changes["business_point_id"] = point_id
                if not changes:
                    continue
                points[point_id] = changes
            else:
                points[point_id] = current
        if points or parent is None:
            definition["points"] = points
        result[str(table_id)] = definition
    return {"point_tables": result}
