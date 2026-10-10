"""Shared Domain 点表与业务点模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType

from .identities import BusinessPointId, PointTableId
from .unit import Unit
from .value_objects import DataType, PointAccess, Protocol


@dataclass(frozen=True, slots=True)
class BusinessPoint:
    """稳定业务点实体，回答“这个量是什么”。"""

    business_point_id: BusinessPointId
    data_type: DataType
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

    def coerce(self, value: object) -> bool | int | float | str:
        """按业务点标准数据类型收敛值。"""
        return self.data_type.coerce(value)


@dataclass(frozen=True, slots=True)
class Point:
    """PointTable 内的一条设备点定义。

    稳定字段由 Domain 明确定义；协议专有字段仅作为不透明 ext 保存，
    其含义、校验和转换全部由对应 Infrastructure Adapter 解释。
    """

    point_id: str
    business_point_id: BusinessPointId
    source_unit: Unit
    access: PointAccess
    scale: float = 1.0
    offset: float = 0.0
    ext: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

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

        ext = dict(self.ext)
        for key, value in ext.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("point ext keys must be non-empty strings")
            if value is not None and not isinstance(
                value,
                str | int | float | bool,
            ):
                raise ValueError(f"point ext '{key}' must be a scalar value or null")
            if isinstance(value, float) and not isfinite(value):
                raise ValueError(f"point ext '{key}' must be finite")

        object.__setattr__(self, "point_id", point_id)
        object.__setattr__(
            self,
            "business_point_id",
            BusinessPointId(business_point_id),
        )
        object.__setattr__(self, "ext", MappingProxyType(ext))


PointScalarValue = float | int | bool | str | None
WritableScalarValue = float | int | bool | str


def engineering_value(point: Point, raw: PointScalarValue) -> PointScalarValue:
    """按点表 scale/offset 把协议原始值换算为工程值。

    只转换 int/float 且排除 bool；None、字符串原样保留。
    """
    if raw is None or isinstance(raw, bool | str) or (point.scale == 1.0 and point.offset == 0.0):
        return raw
    return raw * point.scale + point.offset


def raw_write_value(point: Point, value: WritableScalarValue) -> WritableScalarValue:
    """把工程写值按点表 scale/offset 逆变换为协议原始值。

    只转换 int/float 且排除 bool；其余类型原样下发，由协议 Driver
    按点 data_type 编码并校验。
    """
    if isinstance(value, bool | str):
        return value
    if point.scale == 1.0 and point.offset == 0.0:
        return value
    return (value - point.offset) / point.scale


@dataclass(frozen=True, slots=True)
class PointMeta:
    """点位的进程级元数据（采集分组与诊断/展示用，不属于协议寻址）。

    Attributes:
        variable_name: 业务变量名（状态/诊断输出展示）。
        point_groups: 采集分组集合——Task 按 point_group 选点，诊断按
            point_group 批量验证。
    """

    variable_name: str | None
    point_groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PointTable:
    """某类设备在一种 Protocol 下的可复用点表。"""

    point_table_id: PointTableId
    protocol: Protocol
    points: Mapping[str, Point]
    parent_id: PointTableId | None = None

    def __post_init__(self) -> None:
        point_table_id = self.point_table_id.strip()
        if not point_table_id:
            raise ValueError("point_table_id must not be empty")

        if self.parent_id is not None:
            parent_id = self.parent_id.strip()
            if not parent_id or parent_id == point_table_id:
                raise ValueError("parent_id must be non-empty and different from point_table_id")
            object.__setattr__(self, "parent_id", PointTableId(parent_id))

        points = dict(self.points)
        for point_id, point in points.items():
            if not point_id:
                raise ValueError("point table keys must not be empty")
            if point_id != point_id.strip():
                raise ValueError("point table keys must not contain surrounding whitespace")
            if point_id != point.point_id:
                raise ValueError(
                    f"point table key '{point_id}' does not match " f"point_id '{point.point_id}'"
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

    def point(self, point_id: str) -> Point:
        """按本地点 ID 返回协议点。"""
        return self.points[point_id.strip()]

    def points_for_business(
        self,
        business_point_id: BusinessPointId,
    ) -> tuple[Point, ...]:
        """返回映射到同一业务点的全部协议点。"""
        return tuple(
            point for point in self.points.values() if point.business_point_id == business_point_id
        )
