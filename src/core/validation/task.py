"""采集任务配置引用一致性校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.config import CollectionTask, PointSet, PointSetId, SinkDefinition, SinkId
from core.domain import Device, DeviceGroup, DeviceGroupId, DeviceId


def validate_collection_tasks(
    tasks: Sequence[CollectionTask],
    devices: Mapping[DeviceId, Device],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
    point_sets: Mapping[PointSetId, PointSet],
    sinks: Mapping[SinkId, SinkDefinition],
) -> None:
    """校验 CollectionTask 的已知静态引用关系。

    校验 Device / DeviceGroup / PointSet / Sink 的静态引用完整性。
    """
    for task in tasks:
        if task.device_id is not None and task.device_id not in devices:
            raise ValueError(
                f"task '{task.task_id}' references unknown device '{task.device_id}'"
            )
        if (
            task.device_group_id is not None
            and task.device_group_id not in device_groups
        ):
            raise ValueError(
                f"task '{task.task_id}' references unknown device group "
                f"'{task.device_group_id}'"
            )
        if task.point_set_id not in point_sets:
            raise ValueError(
                f"task '{task.task_id}' references unknown point set "
                f"'{task.point_set_id}'"
            )
        for sink_id in task.target_sink_ids:
            sink = sinks.get(sink_id)
            if sink is None:
                raise ValueError(
                    f"task '{task.task_id}' references unknown sink '{sink_id}'"
                )
            if not sink.enabled:
                raise ValueError(
                    f"task '{task.task_id}' references disabled sink '{sink_id}'"
                )
