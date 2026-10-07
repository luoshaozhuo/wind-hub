"""Shared Domain 实体。

BusinessPoint 表达稳定业务点；ProtocolPoint 表达协议点表中的具体点定义。
PointTable 绑定一种协议，并通过 ProtocolPoint.business_point_id 关联业务点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .value_objects import ProtocolType, Unit


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """返回映射的只读浅拷贝。"""
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class DeviceGroup:
    """设备业务分组，可被多台设备复用。"""

    group_id: str
    name: str | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        group_id = self.group_id.strip()
        if not group_id:
            raise ValueError("group_id must not be empty")
        object.__setattr__(self, "group_id", group_id)


@dataclass(frozen=True, slots=True)
class BusinessPoint:
    """稳定业务点定义，回答“这个量是什么”。

    unit 是业务标准单位，与具体协议、地址和点表解耦。
    """

    business_point_id: str
    name: str
    unit: Unit
    description: str | None = None

    def __post_init__(self) -> None:
        point_id = self.business_point_id.strip()
        name = self.name.strip()
        if not point_id:
            raise ValueError("business_point_id must not be empty")
        if not name:
            raise ValueError("business point name must not be empty")
        object.__setattr__(self, "business_point_id", point_id)
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class ProtocolPoint:
    """协议点表中的具体点定义，回答“通过该协议如何得到这个量”。

    公共属性保存数据类型、单位、缩放、偏移和读写能力；ext 只保存协议特有
    属性，例如 Modbus register、ADS symbol、IEC 104 IOA。
    """

    point_id: str
    business_point_id: str
    data_type: str
    unit: Unit
    scale: float = 1.0
    offset: float = 0.0
    readable: bool = True
    writable: bool = False
    ext: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        point_id = self.point_id.strip()
        business_point_id = self.business_point_id.strip()
        data_type = self.data_type.strip().lower()
        if not point_id:
            raise ValueError("point_id must not be empty")
        if not business_point_id:
            raise ValueError("business_point_id must not be empty")
        if not data_type:
            raise ValueError("data_type must not be empty")
        object.__setattr__(self, "point_id", point_id)
        object.__setattr__(self, "business_point_id", business_point_id)
        object.__setattr__(self, "data_type", data_type)
        object.__setattr__(self, "ext", _freeze_mapping(self.ext))


@dataclass(frozen=True, slots=True)
class PointTable:
    """绑定单一协议的一套可复用点表。"""

    point_table_id: str
    name: str
    protocol: ProtocolType
    points: tuple[ProtocolPoint, ...]

    def __post_init__(self) -> None:
        table_id = self.point_table_id.strip()
        name = self.name.strip()
        if not table_id:
            raise ValueError("point_table_id must not be empty")
        if not name:
            raise ValueError("point table name must not be empty")
        point_ids = [point.point_id for point in self.points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError(f"point table '{table_id}' contains duplicate point_id")
        object.__setattr__(self, "point_table_id", table_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "points", tuple(self.points))


@dataclass(frozen=True, slots=True)
class DeviceModel:
    """设备型号定义；多个现场设备可复用同一型号和点表。"""

    model_id: str
    device_type: str
    point_table_id: str
    manufacturer: str | None = None
    model_name: str | None = None
    properties: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        model_id = self.model_id.strip()
        device_type = self.device_type.strip()
        point_table_id = self.point_table_id.strip()
        if not model_id:
            raise ValueError("model_id must not be empty")
        if not device_type:
            raise ValueError("device_type must not be empty")
        if not point_table_id:
            raise ValueError("point_table_id must not be empty")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "device_type", device_type)
        object.__setattr__(self, "point_table_id", point_table_id)
        object.__setattr__(self, "properties", _freeze_mapping(self.properties))


@dataclass(frozen=True, slots=True)
class Device:
    """现场具体设备，只表达业务身份和静态归属。"""

    device_id: str
    model_id: str
    group_ids: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        device_id = self.device_id.strip()
        model_id = self.model_id.strip()
        if not device_id:
            raise ValueError("device_id must not be empty")
        if not model_id:
            raise ValueError("model_id must not be empty")
        groups = tuple(group_id.strip() for group_id in self.group_ids)
        if any(not group_id for group_id in groups):
            raise ValueError("group_ids must not contain empty values")
        if len(groups) != len(set(groups)):
            raise ValueError("group_ids must not contain duplicates")
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "group_ids", groups)
