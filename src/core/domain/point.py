"""Shared Domain 点表与业务点模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from .identities import BusinessPointId, PointTableId
from .unit import Unit
from .value_objects import PointAccess, Protocol, RawDataType, ValueType


@dataclass(frozen=True, slots=True)
class BusinessPoint:
    """稳定业务点实体，回答“这个量是什么”。"""

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
    """PointTable 内的一条协议点语义定义。

    这里只表达跨协议稳定语义；Modbus 地址、ADS symbol、IEC104 IOA 等
    协议专有寻址信息不属于 Domain。
    """

    point_id: str
    business_point_id: BusinessPointId
    raw_type: RawDataType
    source_unit: Unit
    access: PointAccess
    scale: float = 1.0
    offset: float = 0.0

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


@dataclass(frozen=True, slots=True)
class PointTable:
    """某类设备在一种 Protocol 下的可复用点表。"""

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
                raise ValueError(
                    "point table keys must not contain surrounding whitespace"
                )
            if point_id != point.point_id:
                raise ValueError(
                    f"point table key '{point_id}' does not match "
                    f"point_id '{point.point_id}'"
                )

        object.__setattr__(
            self,
            "point_table_id",
            PointTableId(point_table_id),
        )
        object.__setattr__(
            self,
            "points",
            MappingProxyType(points),
        )

    def point(self, point_id: str) -> ProtocolPoint:
        """按本地点 ID 返回协议点。"""
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
