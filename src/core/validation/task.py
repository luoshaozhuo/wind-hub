"""采集任务配置引用一致性校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from core.config import CollectionTask, PointSet, PointSetId
from core.domain import Device, DeviceGroup, DeviceGroupId, DeviceId


def validate_collection_tasks(
    tasks: Sequence[CollectionTask],
    devices: Mapping[DeviceId, Device],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
    point_sets: Mapping[PointSetId, PointSet],
) -> None:
    """校验 CollectionTask 的已知静态引用关系。

    Sink 引用在 Sink 配置模型建立后再由统一快照校验补充；本函数当前只校验
    已经稳定的 Device / DeviceGroup / PointSet 引用。
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
