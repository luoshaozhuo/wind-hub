"""点与单位相关跨聚合引用一致性校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.config import PointSet
from core.domain import (
    BusinessPoint,
    BusinessPointId,
    PointTable,
    PointTableId,
    Quantity,
    ValueType,
)


def validate_business_points(
    business_points: Mapping[BusinessPointId, BusinessPoint],
) -> None:
    """校验稳定业务点自身的跨值对象语义约束。"""
    for point in business_points.values():
        if (
            point.value_type in (ValueType.BOOLEAN, ValueType.STRING)
            and point.standard_unit.quantity is not Quantity.DIMENSIONLESS
        ):
            raise ValueError(
                f"business point '{point.business_point_id}' with value type "
                f"'{point.value_type}' must use a dimensionless standard unit"
            )


def validate_point_tables(
    point_tables: Sequence[PointTable],
    business_points: Mapping[BusinessPointId, BusinessPoint],
) -> None:
    """校验 PointTable 内 ProtocolPoint 的跨聚合引用与单位兼容性。

    Args:
        point_tables: 待校验的点表集合。
        business_points: 可引用的稳定业务点映射。

    Raises:
        ValueError: 点表 ID 重复、业务点不存在或源单位与标准单位类别不兼容。
    """
    seen_table_ids: set[PointTableId] = set()

    for table in point_tables:
        if table.point_table_id in seen_table_ids:
            raise ValueError(f"duplicate point table id '{table.point_table_id}'")
        seen_table_ids.add(table.point_table_id)

        for point in table.points.values():
            business_point = business_points.get(point.business_point_id)
            if business_point is None:
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"references unknown business point '{point.business_point_id}'"
                )

            if point.source_unit.quantity != business_point.standard_unit.quantity:
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"uses source quantity '{point.source_unit.quantity}', but business point "
                    f"'{business_point.business_point_id}' expects "
                    f"'{business_point.standard_unit.quantity}'"
                )

            if (
                business_point.value_type in (ValueType.BOOLEAN, ValueType.STRING)
                and (point.scale != 1.0 or point.offset != 0.0)
            ):
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"maps {business_point.value_type} business point "
                    "with non-identity scale/offset"
                )


def validate_point_sets(
    point_sets: Sequence[PointSet],
    business_points: Mapping[BusinessPointId, BusinessPoint],
) -> None:
    """校验 PointSet 对 BusinessPoint 的引用完整性。"""
    for point_set in point_sets:
        for business_point_id in point_set.business_point_ids:
            if business_point_id not in business_points:
                raise ValueError(
                    f"point set '{point_set.point_set_id}' references unknown business point "
                    f"'{business_point_id}'"
                )
