"""采集任务领域模型，仅描述采集设备组、测点组与周期。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from .device import Device, DeviceGroup
from .identities import DeviceGroupId, DeviceId


@dataclass(frozen=True, slots=True)
class Task:
    """按设备分组和测点分组执行的独立周期采集任务。"""

    task_id: str
    device_group_id: DeviceGroupId
    point_group: str
    interval: float
    enabled: bool = True

    def __post_init__(self) -> None:
        for label, value in (
            ("task_id", self.task_id),
            ("device_group_id", self.device_group_id),
            ("point_group", self.point_group),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be a non-empty string")
        if isinstance(self.interval, bool) or not isinstance(self.interval, int | float):
            raise ValueError("interval must be a positive finite number")
        if not isfinite(self.interval) or self.interval <= 0:
            raise ValueError("interval must be a positive finite number")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        object.__setattr__(self, "task_id", self.task_id.strip())
        object.__setattr__(self, "device_group_id", DeviceGroupId(self.device_group_id.strip()))
        object.__setattr__(self, "point_group", self.point_group.strip())
        object.__setattr__(self, "interval", float(self.interval))


def validate_task_references(
    tasks: Mapping[str, Task],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
) -> None:
    """Validate acquisition task identity and device-group references."""
    for task_id, task in tasks.items():
        if task_id != task.task_id:
            raise ValueError(f"task key '{task_id}' does not match task_id '{task.task_id}'")
        if task.device_group_id not in device_groups:
            raise ValueError(
                f"task '{task_id}' references unknown device group '{task.device_group_id}'"
            )


def devices_for_task(
    task: Task,
    devices: Mapping[DeviceId, Device],
) -> tuple[Device, ...]:
    """选出属于任务设备组的设备；设备可同时参与不同任务。"""
    return tuple(
        device for device in devices.values() if task.device_group_id in device.device_group_ids
    )
