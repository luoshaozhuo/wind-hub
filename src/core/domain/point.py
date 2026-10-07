"""Shared Domain 点与点表模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from .identities import BusinessPointId, PointTableId
from .unit import Unit
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

    business_point_id 同时承担稳定身份与业务名称，不再保存重复 name 字段。
    业务点与具体协议、地址和点表解耦；standard_unit 是内置工程单位值对象，
    value_type 是转换后的标准业务值类型。
    """

    business_point_id: BusinessPointId
    value_type: ValueType
    standard_unit: Unit
    description: str | None = None

    def __post_init__(self) -> None:
        business_point_id = self.business_point_id.strip()
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        object.__setattr__(
            self,
            "business_point_id",
            BusinessPointId(business_point_id),
        )


@dataclass(frozen=True, slots=True)
class ProtocolPoint:
    """PointTable 内部的协议点实体。

    point_id 仅在所属 PointTable 聚合内具有身份意义，不是全局 ID。
    business_point_id 关联稳定业务点；raw_type、source_unit、scale、offset
    描述从协议原始量到标准业务量的解释规则；protocol_options 只保存协议
    专有标量字段，例如 Modbus 地址、ADS Symbol 或 IEC 104 IOA。

    scale 与 offset 仅描述协议点映射参数。单位兼容性需要结合所关联
    BusinessPoint.standard_unit 判断，不由 ProtocolPoint 单独执行。
    """

    point_id: str
    business_point_id: BusinessPointId
    raw_type: RawDataType
    source_unit: Unit
    access: PointAccess
    scale: float = 1.0
    offset: float = 0.0
    protocol_options: Mapping[str, ProtocolOptionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        business_point_id = self.business_point_id.strip()
        if not point_id:
            raise ValueError("point_id must not be empty")
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        if not isfinite(self.scale) or self.scale == 0.0:
            raise ValueError("scale must be finite and non-zero")
        if not isfinite(self.offset):
            raise ValueError("offset must be finite")
        object.__setattr__(self, "point_id", point_id)
        object.__setattr__(
            self,
            "business_point_id",
            BusinessPointId(business_point_id),
        )
        object.__setattr__(
            self,
            "protocol_options",
            _freeze_mapping(self.protocol_options),
        )


@dataclass(frozen=True, slots=True)
class PointTable:
    """单一协议下的一套可复用点表聚合根。

    point_table_id 同时承担稳定身份与点表名称，不再保存重复 name 字段。
    PointTable 是 ProtocolPoint 的一致性边界。points 以 point_id 为键，键必须与
    ProtocolPoint.point_id 一致；BusinessPoint 不属于本聚合，ProtocolPoint 仅通过
    business_point_id 引用它。
    """

    point_table_id: PointTableId
    protocol: Protocol
    points: Mapping[str, ProtocolPoint]

    def __post_init__(self) -> None:
        point_table_id = self.point_table_id.strip()
        if not point_table_id:
            raise ValueError("point_table_id must not be empty")

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
