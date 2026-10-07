"""点与单位相关跨聚合引用一致性校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.domain import (
    BusinessPoint,
    BusinessPointId,
    PointTable,
    PointTableId,
    Unit,
    UnitId,
)


def validate_business_points(
    business_points: Mapping[BusinessPointId, BusinessPoint],
    units: Mapping[UnitId, Unit],
) -> None:
    """校验 BusinessPoint 对标准单位的引用完整性。"""
    for point in business_points.values():
        if point.standard_unit_id not in units:
            raise ValueError(
                f"business point '{point.business_point_id}' references unknown unit "
                f"'{point.standard_unit_id}'"
            )


def validate_point_tables(
    point_tables: Sequence[PointTable],
    business_points: Mapping[BusinessPointId, BusinessPoint],
    units: Mapping[UnitId, Unit],
) -> None:
    """校验 PointTable 内 ProtocolPoint 的跨聚合引用完整性。"""
    seen_table_ids: set[PointTableId] = set()

    for table in point_tables:
        if table.point_table_id in seen_table_ids:
            raise ValueError(f"duplicate point table id '{table.point_table_id}'")
        seen_table_ids.add(table.point_table_id)

        for point in table.points.values():
            if point.business_point_id not in business_points:
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"references unknown business point '{point.business_point_id}'"
                )
            if point.source_unit_id not in units:
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"references unknown source unit '{point.source_unit_id}'"
                )
