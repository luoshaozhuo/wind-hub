"""Shared Domain 设备模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """返回映射的只读浅拷贝。"""
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class DeviceType:
    """设备业务类型，例如风机、PCS、BMS 或测风塔。"""

    device_type_id: str
    name: str
    description: str | None = None

    def __post_init__(self) -> None:
        device_type_id = self.device_type_id.strip()
        name = self.name.strip()
        if not device_type_id:
            raise ValueError("device_type_id must not be empty")
        if not name:
            raise ValueError("device type name must not be empty")
        object.__setattr__(self, "device_type_id", device_type_id)
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class DeviceGroup:
    """设备业务分组。

    分组表达业务上的设备集合，不等同于设备类型，也不承载采集运行状态。
    """

    device_group_id: str
    name: str
    description: str | None = None

    def __post_init__(self) -> None:
        device_group_id = self.device_group_id.strip()
        name = self.name.strip()
        if not device_group_id:
            raise ValueError("device_group_id must not be empty")
        if not name:
            raise ValueError("device group name must not be empty")
        object.__setattr__(self, "device_group_id", device_group_id)
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class DeviceModel:
    """可复用的设备型号定义。"""

    device_model_id: str
    device_type_id: str
    point_table_id: str
    name: str | None = None
    manufacturer: str | None = None
    properties: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        device_model_id = self.device_model_id.strip()
        device_type_id = self.device_type_id.strip()
        point_table_id = self.point_table_id.strip()
        if not device_model_id:
            raise ValueError("device_model_id must not be empty")
        if not device_type_id:
            raise ValueError("device_type_id must not be empty")
        if not point_table_id:
            raise ValueError("point_table_id must not be empty")
        object.__setattr__(self, "device_model_id", device_model_id)
        object.__setattr__(self, "device_type_id", device_type_id)
        object.__setattr__(self, "point_table_id", point_table_id)
        object.__setattr__(self, "properties", _freeze_mapping(self.properties))


@dataclass(frozen=True, slots=True)
class Device:
    """现场具体设备。

    只表达设备业务身份和静态归属，不持有 Endpoint、连接或 DeviceSession。
    """

    device_id: str
    device_model_id: str
    name: str | None = None
    device_group_ids: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        device_id = self.device_id.strip()
        device_model_id = self.device_model_id.strip()
        if not device_id:
            raise ValueError("device_id must not be empty")
        if not device_model_id:
            raise ValueError("device_model_id must not be empty")
        group_ids = tuple(group_id.strip() for group_id in self.device_group_ids)
        if any(not group_id for group_id in group_ids):
            raise ValueError("device_group_ids must not contain empty values")
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("device_group_ids must not contain duplicates")
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "device_model_id", device_model_id)
        object.__setattr__(self, "device_group_ids", group_ids)
