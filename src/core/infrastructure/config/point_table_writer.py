"""从完整 PointTable 领域对象还原继承式 points.yaml。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from core.domain import Point, PointTable, PointTableId


def _point_dict(point: Point) -> dict[str, Any]:
    address = dict(point.ext)
    data_type = address.pop("data_type")
    result: dict[str, Any] = {
        "point_groups": list(point.point_groups),
        "business_point_id": str(point.business_point_id),
        "address": address,
        "data_type": data_type,
        "scale": point.scale,
        "offset": point.offset,
        "unit": point.source_unit.unit_id,
        "description": point.description,
    }
    return result


def dump_point_tables(
    tables: Mapping[PointTableId, PointTable],
    *,
    definitions: Mapping[str, Mapping[str, Any]] | None = None,
    baseline: Mapping[PointTableId, PointTable] | None = None,
) -> dict[str, Any]:
    """输出继承式 YAML；保留显式覆盖字段，且与当前有效点集一致。"""
    result: dict[str, Any] = {}
    for table_id, table in tables.items():
        # 对未变化的点表保留原始配置；变化时仍保留原来显式声明的字段。
        parent_unchanged = (
            table.parent_id is None
            or (baseline is not None and tables.get(table.parent_id) == baseline.get(table.parent_id))
        )
        if (
            definitions is not None
            and baseline is not None
            and str(table_id) in definitions
            and baseline.get(table_id) == table
            and parent_unchanged
        ):
            result[str(table_id)] = deepcopy(dict(definitions[str(table_id)]))
            continue
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
                original = (definitions or {}).get(str(table_id), {}).get("points", {}).get(point_id, {})
                for field in original:
                    if field in current:
                        # 即使与当前父表相等，也保留显式覆盖；父表更新不会
                        # 意外改变子表已指定字段的继承语义。
                        changes[field] = current[field]
                if not changes:
                    continue
                points[point_id] = changes
            else:
                points[point_id] = current
        if points or parent is None:
            definition["points"] = points
        result[str(table_id)] = definition
    return {"point_tables": result}
