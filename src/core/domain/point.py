"""Shared Domain 点与点表模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeAlias

from .identities import BusinessPointId, PointTableId, UnitId
from .value_objects import PointAccess, Protocol, RawDataType, ValueType

ProtocolOptionValue: TypeAlias = str | int | float | bool | None


def _freeze_mapping(
    value: Mapping[str, ProtocolOptionValue],
) -> Mapping[str, ProtocolOptionValue]:
    """返回协议专有字段的只读浅拷贝。"""
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class BusinessPoint:
    """稳定业务点实体，回答“这个量是什么”。

    业务点与具体协议、地址和点表解耦。standard_unit_id 引用系统内标准单位，
    value_type 是转换后的标准业务值类型。
    """

    business_point_id: BusinessPointId
    name: str
    value_type: ValueType
    standard_unit_id: UnitId
    description: str | None = None

    def __post_init__(self) -> None:
        business_point_id = self.business_point_id.strip()
        standard_unit_id = self.standard_unit_id.strip().lower()
        name = self.name.strip()
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        if not standard_unit_id:
            raise ValueError("standard_unit_id must not be empty")
        if not name:
            raise ValueError("business point name must not be empty")
        object.__setattr__(
            self,
            "business_point_id",
            BusinessPointId(business_point_id),
        )
        object.__setattr__(self, "standard_unit_id", UnitId(standard_unit_id))
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class ProtocolPoint:
    """PointTable 内部的协议点实体。

    point_id 仅在所属 PointTable 聚合内具有身份意义，不是全局 ID。
    business_point_id 关联稳定业务点；raw_type、source_unit_id、scale、offset
    描述从协议原始量到标准业务量的解释规则；protocol_options 只保存协议
    专有标量字段，例如 Modbus 地址、ADS Symbol 或 IEC 104 IOA。

    scale 与 offset 仅描述映射参数。映射是否合法以及如何转换，需要结合所关联
    BusinessPoint 的 value_type 与 standard_unit_id 判断，不由 ProtocolPoint 单独执行。
    """

    point_id: str
    business_point_id: BusinessPointId
    raw_type: RawDataType
    source_unit_id: UnitId
    access: PointAccess
    scale: float = 1.0
    offset: float = 0.0
    protocol_options: Mapping[str, ProtocolOptionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        business_point_id = self.business_point_id.strip()
        source_unit_id = self.source_unit_id.strip().lower()
        if not point_id:
            raise ValueError("point_id must not be empty")
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        if not source_unit_id:
            raise ValueError("source_unit_id must not be empty")
        object.__setattr__(self, "point_id", point_id)
        object.__setattr__(
            self,
            "business_point_id",
            BusinessPointId(business_point_id),
        )
        object.__setattr__(self, "source_unit_id", UnitId(source_unit_id))
        object.__setattr__(
            self,
            "protocol_options",
            _freeze_mapping(self.protocol_options),
        )


@dataclass(frozen=True, slots=True)
class PointTable:
    """单一协议下的一套可复用点表聚合根。

    PointTable 是 ProtocolPoint 的一致性边界。points 以 point_id 为键，键必须与
    ProtocolPoint.point_id 一致；BusinessPoint 不属于本聚合，ProtocolPoint 仅通过
    business_point_id 引用它。
    """

    point_table_id: PointTableId
    name: str
    protocol: Protocol
    points: Mapping[str, ProtocolPoint]

    def __post_init__(self) -> None:
        point_table_id = self.point_table_id.strip()
        name = self.name.strip()
        if not point_table_id:
            raise ValueError("point_table_id must not be empty")
        if not name:
            raise ValueError("point table name must not be empty")

        points = dict(self.points)
        for point_id, point in points.items():
            if not point_id:
                raise ValueError("point table keys must not be empty")
            if point_id != point_id.strip():
                raise ValueError("point table keys must not contain surrounding whitespace")
            if point_id != point.point_id:
                raise ValueError(
                    f"point table key '{point_id}' does not match point_id "
                    f"'{point.point_id}'"
                )

        object.__setattr__(self, "point_table_id", PointTableId(point_table_id))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "points", MappingProxyType(points))

    def point(self, point_id: str) -> ProtocolPoint:
        """按表内 point_id 返回协议点。"""
        return self.points[point_id.strip()]

    def points_for_business(
        self,
        business_point_id: BusinessPointId,
    ) -> tuple[ProtocolPoint, ...]:
        """返回映射到同一业务点的全部协议点。"""
        return tuple(
            point
            for point in self.points.values()
            if point.business_point_id == business_point_id
        )
