"""Shared Core 协议接入配置模型。

这些对象描述设备接入时如何解释协议点，不属于业务 Domain：
- Protocol：协议标识；
- RawDataType：协议原始数据类型；
- PointAccess：协议点访问能力；
- ProtocolPoint：业务点与协议点的映射；
- PointTable：某协议下可复用的协议点表。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from core.domain import BusinessPointId, Unit

from .identities import PointTableId

ProtocolOptionValue: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class Protocol:
    """协议稳定标识，不包含连接或运行时能力。"""

    name: str

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not name:
            raise ValueError("protocol name must not be empty")
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class RawDataType:
    """协议侧原始数据类型标识。"""

    name: str

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not name:
            raise ValueError("raw data type must not be empty")
        object.__setattr__(self, "name", name)


class PointAccess(StrEnum):
    """协议点访问能力。"""

    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


def _freeze_options(
    value: Mapping[str, ProtocolOptionValue],
) -> Mapping[str, ProtocolOptionValue]:
    options = dict(value)
    for key, item in options.items():
        if isinstance(item, float) and not isfinite(item):
            raise ValueError(
                f"protocol option '{key}' must be finite"
            )
    return MappingProxyType(options)


@dataclass(frozen=True, slots=True)
class ProtocolPoint:
    """PointTable 内的一条协议点映射配置。"""

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
            _freeze_options(self.protocol_options),
        )


@dataclass(frozen=True, slots=True)
class PointTable:
    """单一协议下的一套可复用协议点表。"""

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
        return self.points[point_id.strip()]

    def points_for_business(
        self,
        business_point_id: BusinessPointId,
    ) -> tuple[ProtocolPoint, ...]:
        return tuple(
            point
            for point in self.points.values()
            if point.business_point_id == business_point_id
        )
