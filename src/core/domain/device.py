"""Shared Domain 设备模型。"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .identities import (
    DeviceGroupId,
    DeviceId,
    DeviceModelId,
    DeviceTypeId,
    PointTableId,
)


@dataclass(frozen=True, slots=True)
class DeviceType:
    """设备业务类型，例如风机、PCS、BMS 或测风塔。"""

    device_type_id: DeviceTypeId
    name: str
    description: str | None = None

    def __post_init__(self) -> None:
        device_type_id = self.device_type_id.strip()
        name = self.name.strip()
        if not device_type_id:
            raise ValueError("device_type_id must not be empty")
        if not name:
            raise ValueError("device type name must not be empty")
        object.__setattr__(self, "device_type_id", DeviceTypeId(device_type_id))
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class DeviceGroup:
    """设备业务分组。

    分组表达业务上的设备集合，不等同于设备类型，也不承载采集运行状态。
    """

    device_group_id: DeviceGroupId
    name: str
    description: str | None = None

    def __post_init__(self) -> None:
        device_group_id = self.device_group_id.strip()
        name = self.name.strip()
        if not device_group_id:
            raise ValueError("device_group_id must not be empty")
        if not name:
            raise ValueError("device group name must not be empty")
        object.__setattr__(self, "device_group_id", DeviceGroupId(device_group_id))
        object.__setattr__(self, "name", name)


@dataclass(frozen=True, slots=True)
class DeviceModel:
    """可复用的设备型号聚合根。

    一个 DeviceModel 固定对应一张 PointTable，这是设备型号对外数据语义的一部分。
    """

    device_model_id: DeviceModelId
    device_type_id: DeviceTypeId
    point_table_id: PointTableId
    name: str | None = None
    manufacturer: str | None = None

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

        object.__setattr__(self, "device_model_id", DeviceModelId(device_model_id))
        object.__setattr__(self, "device_type_id", DeviceTypeId(device_type_id))
        object.__setattr__(self, "point_table_id", PointTableId(point_table_id))


@dataclass(frozen=True, slots=True)
class Device:
    """现场具体设备聚合根。

    Device 只维护设备自身业务身份、型号引用和业务分组引用。通信端点、协议会话、
    启停配置等不属于本聚合。
    """

    device_id: DeviceId
    device_model_id: DeviceModelId
    name: str | None = None
    device_group_ids: tuple[DeviceGroupId, ...] = ()

    def __post_init__(self) -> None:
        device_id = self.device_id.strip()
        device_model_id = self.device_model_id.strip()
        name = self.name.strip() if self.name is not None else None

        if not device_id:
            raise ValueError("device_id must not be empty")
        if not device_model_id:
            raise ValueError("device_model_id must not be empty")
        if name == "":
            name = None

        group_ids = tuple(DeviceGroupId(group_id.strip()) for group_id in self.device_group_ids)
        if any(not group_id for group_id in group_ids):
            raise ValueError("device_group_ids must not contain empty values")
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("device_group_ids must not contain duplicates")

        object.__setattr__(self, "device_id", DeviceId(device_id))
        object.__setattr__(self, "device_model_id", DeviceModelId(device_model_id))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "device_group_ids", group_ids)

    def rename(self, name: str | None) -> Device:
        """返回修改名称后的新聚合快照。"""
        return replace(self, name=name)

    def change_model(self, device_model_id: DeviceModelId) -> Device:
        """返回切换型号引用后的新聚合快照。"""
        return replace(self, device_model_id=device_model_id)

    def assign_group(self, device_group_id: DeviceGroupId) -> Device:
        """将设备加入业务分组；重复加入保持幂等。"""
        group_id = DeviceGroupId(device_group_id.strip())
        if not group_id:
            raise ValueError("device_group_id must not be empty")
        if group_id in self.device_group_ids:
            return self
        return replace(self, device_group_ids=(*self.device_group_ids, group_id))

    def remove_group(self, device_group_id: DeviceGroupId) -> Device:
        """将设备移出业务分组；不存在时保持幂等。"""
        group_id = DeviceGroupId(device_group_id.strip())
        if not group_id:
            raise ValueError("device_group_id must not be empty")
        if group_id not in self.device_group_ids:
            return self
        return replace(
            self,
            device_group_ids=tuple(
                existing for existing in self.device_group_ids if existing != group_id
            ),
        )
