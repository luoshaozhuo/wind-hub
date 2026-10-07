"""采集任务静态配置。"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain import DeviceGroupId, DeviceId

from .identities import PointSetId, SinkId, TaskId


@dataclass(frozen=True, slots=True)
class CollectionTask:
    """持续采集任务定义。

    CollectionTask 只描述静态意图：
    - 采集哪一台 Device，或哪一个 DeviceGroup；
    - 采集哪个可复用 PointSet；
    - 采集节拍；
    - 输出到哪些 Sink；
    - 是否允许 Runtime 为其创建实例。

    Task Instance、RUNNING/STOPPED、调度句柄、订阅句柄、失败次数等均属于 Runtime。
    """

    task_id: TaskId
    point_set_id: PointSetId
    target_sink_ids: tuple[SinkId, ...]
    device_id: DeviceId | None = None
    device_group_id: DeviceGroupId | None = None
    interval: float | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        task_id = self.task_id.strip()
        point_set_id = self.point_set_id.strip()
        device_id = self.device_id.strip() if self.device_id is not None else None
        device_group_id = (
            self.device_group_id.strip() if self.device_group_id is not None else None
        )
        target_sink_ids = tuple(SinkId(sink_id.strip()) for sink_id in self.target_sink_ids)

        if not task_id:
            raise ValueError("task_id must not be empty")
        if not point_set_id:
            raise ValueError("point_set_id must not be empty")
        if (device_id is None) == (device_group_id is None):
            raise ValueError(
                "exactly one of device_id and device_group_id must be configured"
            )
        if device_id == "":
            raise ValueError("device_id must not be empty")
        if device_group_id == "":
            raise ValueError("device_group_id must not be empty")
        if not target_sink_ids:
            raise ValueError("target_sink_ids must not be empty")
        if any(not sink_id for sink_id in target_sink_ids):
            raise ValueError("target_sink_ids must not contain empty values")
        if len(target_sink_ids) != len(set(target_sink_ids)):
            raise ValueError("target_sink_ids must not contain duplicates")
        if self.interval is not None and self.interval <= 0:
            raise ValueError("interval must be greater than zero")

        object.__setattr__(self, "task_id", TaskId(task_id))
        object.__setattr__(self, "point_set_id", PointSetId(point_set_id))
        object.__setattr__(
            self,
            "device_id",
            DeviceId(device_id) if device_id is not None else None,
        )
        object.__setattr__(
            self,
            "device_group_id",
            DeviceGroupId(device_group_id) if device_group_id is not None else None,
        )
        object.__setattr__(self, "target_sink_ids", target_sink_ids)
