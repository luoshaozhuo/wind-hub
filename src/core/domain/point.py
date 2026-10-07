"""Shared Domain 点与点表模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, TypeAlias

from .value_objects import PointAccess, Protocol, RawDataType, Unit, ValueType

ProtocolOptionValue: TypeAlias = str | int | float | bool | None


def _freeze_mapping(
    value: Mapping[str, ProtocolOptionValue],
) -> Mapping[str, ProtocolOptionValue]:
    """返回协议专有字段的只读浅拷贝。"""
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class BusinessPoint:
    """稳定业务点定义，回答“这个量是什么”。

    业务点与具体协议、地址和点表解耦。standard_unit 是系统内的标准单位，
    value_type 是转换后的标准业务值类型。
    """

    business_point_id: str
    name: str
    value_type: ValueType
    standard_unit: Unit
    description: str | None = None

    def __post_init__(self) -> None:
        business_point_id = self.business_point_id.strip()
        name = self.name.strip()
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        if not name:
            raise ValueError("business point name must not be empty")
        object.__setattr__(self, "business_point_id", business_point_id)
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class ProtocolPoint:
    """PointTable 内部的协议点实体。

    business_point_id 关联稳定业务点；raw_type、source_unit、scale、offset
    描述从协议原始量到标准业务量的解释规则；protocol_options 只保存协议
    专有标量字段，例如 Modbus 地址、ADS Symbol 或 IEC 104 IOA。

    scale 与 offset 仅描述映射参数。映射是否合法以及如何转换，需要结合所关联
    BusinessPoint 的 value_type 与 standard_unit 判断，不由 ProtocolPoint 单独执行。
    """

    point_id: str
    business_point_id: str
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
        object.__setattr__(self, "point_id", point_id)
        object.__setattr__(self, "business_point_id", business_point_id)
        object.__setattr__(
            self,
            "protocol_options",
            _freeze_mapping(self.protocol_options),
        )


@dataclass(frozen=True, slots=True)
class PointTable:
    """单一协议下的一套可复用点表聚合。

    PointTable 是 ProtocolPoint 的一致性边界；表内 point_id 必须唯一。
    BusinessPoint 不属于本聚合，ProtocolPoint 仅通过 business_point_id 引用它。
    """

    point_table_id: str
    name: str
    protocol: Protocol
    points: tuple[ProtocolPoint, ...]

    def __post_init__(self) -> None:
        point_table_id = self.point_table_id.strip()
        name = self.name.strip()
        if not point_table_id:
            raise ValueError("point_table_id must not be empty")
        if not name:
            raise ValueError("point table name must not be empty")

        points = tuple(self.points)
        point_ids = [point.point_id for point in points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError(
                f"point table '{point_table_id}' contains duplicate point_id"
            )

        object.__setattr__(self, "point_table_id", point_table_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "points", points)

    def point(self, point_id: str) -> ProtocolPoint:
        """按表内 point_id 返回协议点。"""
        for point in self.points:
            if point.point_id == point_id:
                return point
        raise KeyError(point_id)

    def points_for_business(self, business_point_id: str) -> tuple[ProtocolPoint, ...]:
        """返回映射到同一业务点的全部协议点。"""
        return tuple(
            point
            for point in self.points
            if point.business_point_id == business_point_id
        )
