"""采集任务领域模型，不包含配置解析和运行状态。"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from math import isfinite

from .device import Device, DeviceGroup
from .identities import DeviceGroupId, DeviceId


@dataclass(frozen=True, slots=True)
class Task:
    """按设备分组和测点分组执行的周期采集任务。"""

    task_id: str
    device_group_id: DeviceGroupId
    point_group: str
    sink_ids: tuple[str, ...]
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

        sink_ids = tuple(self.sink_ids)
        if not sink_ids or any(not isinstance(item, str) or not item.strip() for item in sink_ids):
            raise ValueError("sink_ids must contain at least one non-empty sink ID")
        if len(sink_ids) != len(set(sink_ids)):
            raise ValueError("sink_ids must not contain duplicates")
        if isinstance(self.interval, bool) or not isinstance(self.interval, int | float):
            raise ValueError("interval must be a positive finite number")
        if not isfinite(self.interval) or self.interval <= 0:
            raise ValueError("interval must be a positive finite number")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")

        object.__setattr__(self, "task_id", self.task_id.strip())
        object.__setattr__(self, "device_group_id", DeviceGroupId(self.device_group_id.strip()))
        object.__setattr__(self, "point_group", self.point_group.strip())
        object.__setattr__(self, "sink_ids", sink_ids)
        object.__setattr__(self, "interval", float(self.interval))


def validate_task_references(
    tasks: Mapping[str, Task],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
    sink_ids: Collection[str],
) -> None:
    """校验任务身份、设备分组及 Sink 引用；不依赖配置或运行时。"""
    available_sinks = set(sink_ids)
    for task_id, task in tasks.items():
        if task_id != task.task_id:
            raise ValueError(f"task key '{task_id}' does not match task_id '{task.task_id}'")
        if task.device_group_id not in device_groups:
            raise ValueError(
                f"task '{task_id}' references unknown device group '{task.device_group_id}'"
            )
        unknown_sinks = set(task.sink_ids) - available_sinks
        if unknown_sinks:
            raise ValueError(f"task '{task_id}' references unknown sinks {sorted(unknown_sinks)}")


def devices_for_task(
    task: Task,
    devices: Mapping[DeviceId, Device],
) -> tuple[Device, ...]:
    """选出属于任务设备组的设备；设备可同时参与不同任务。"""
    return tuple(
        device for device in devices.values()
        if task.device_group_id in device.device_group_ids
    )
