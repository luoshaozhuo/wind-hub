"""Shared Domain 设备模型。"""

from __future__ import annotations

from dataclasses import dataclass


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
    """可复用的设备型号定义。

    一个型号可以声明多张点表，从而表达同一型号通过不同协议暴露数据点的能力。
    具体现场设备启用哪些通信接口不属于该领域对象。
    """

    device_model_id: str
    device_type_id: str
    point_table_ids: tuple[str, ...]
    name: str | None = None
    manufacturer: str | None = None

    def __post_init__(self) -> None:
        device_model_id = self.device_model_id.strip()
        device_type_id = self.device_type_id.strip()
        point_table_ids = tuple(point_table_id.strip() for point_table_id in self.point_table_ids)

        if not device_model_id:
            raise ValueError("device_model_id must not be empty")
        if not device_type_id:
            raise ValueError("device_type_id must not be empty")
        if not point_table_ids:
            raise ValueError("point_table_ids must not be empty")
        if any(not point_table_id for point_table_id in point_table_ids):
            raise ValueError("point_table_ids must not contain empty values")
        if len(point_table_ids) != len(set(point_table_ids)):
            raise ValueError("point_table_ids must not contain duplicates")

        object.__setattr__(self, "device_model_id", device_model_id)
        object.__setattr__(self, "device_type_id", device_type_id)
        object.__setattr__(self, "point_table_ids", point_table_ids)


@dataclass(frozen=True, slots=True)
class Device:
    """现场具体设备。

    只表达设备业务身份和静态归属，不持有启停配置、Endpoint、连接或
    DeviceSession 等运行信息。
    """

    device_id: str
    device_model_id: str
    name: str | None = None
    device_group_ids: tuple[str, ...] = ()

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
