"""采集任务解析为设备级工作计划。

本模块属于 Application：把静态 CollectionTask、Device/DeviceModel/PointTable 与
DeviceConnection 组合为可供后续 placement/runtime 使用的设备级工作单元。
不建立协议连接，不选择具体 Worker，也不执行 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import ConfigSnapshot, SinkId, TaskId
from core.domain import ConnectionId, DeviceId


@dataclass(frozen=True, slots=True)
class CollectionWork:
    """单台设备上的已解析采集工作。

    candidate_connection_ids 是该设备可用的静态接入候选集合。Application 在这里
    不擅自选择其中一个；具体 connection_id 由后续 placement/runtime 根据部署、
    健康状态与调度策略绑定。

    point_ids 已解析为目标 DeviceModel 所绑定 PointTable 中的本地点 ID。
    """

    task_id: TaskId
    device_id: DeviceId
    candidate_connection_ids: tuple[ConnectionId, ...]
    point_ids: tuple[str, ...]
    target_sink_ids: tuple[SinkId, ...]
    interval: float | None


def build_collection_work(
    snapshot: ConfigSnapshot,
    task_id: TaskId,
) -> tuple[CollectionWork, ...]:
    """把一个启用的 CollectionTask 展开为设备级采集工作。

    Args:
        snapshot: 已通过一致性校验的共享静态配置快照。
        task_id: 待解析任务 ID。

    Returns:
        按 device_id 排序的设备级工作集合。禁用任务返回空集合。

    Raises:
        KeyError: task_id 不存在。
        ValueError: 目标设备没有连接，或 PointSet 中业务点无法映射到设备点表。
    """
    task = snapshot.tasks[task_id]
    if not task.enabled:
        return ()

    devices = _resolve_target_devices(snapshot, task_id)
    works = [
        _build_device_work(snapshot, task_id, device_id)
        for device_id in devices
    ]
    return tuple(works)


def _resolve_target_devices(
    snapshot: ConfigSnapshot,
    task_id: TaskId,
) -> tuple[DeviceId, ...]:
    task = snapshot.tasks[task_id]

    if task.device_id is not None:
        return (task.device_id,)

    assert task.device_group_id is not None
    return tuple(
        sorted(
            (
                device.device_id
                for device in snapshot.devices.values()
                if task.device_group_id in device.device_group_ids
            ),
            key=str,
        )
    )


def _build_device_work(
    snapshot: ConfigSnapshot,
    task_id: TaskId,
    device_id: DeviceId,
) -> CollectionWork:
    task = snapshot.tasks[task_id]
    device = snapshot.devices[device_id]
    model = snapshot.device_models[device.device_model_id]
    point_table = snapshot.point_tables[model.point_table_id]
    point_set = snapshot.point_sets[task.point_set_id]

    connection_ids = tuple(
        sorted(
            (
                connection.connection_id
                for connection in snapshot.device_connections.values()
                if connection.device_id == device_id
            ),
            key=str,
        )
    )
    if not connection_ids:
        raise ValueError(
            f"task '{task.task_id}' target device '{device_id}' has no device connection"
        )

    point_ids: list[str] = []
    for business_point_id in point_set.business_point_ids:
        matches = point_table.points_for_business(business_point_id)
        if not matches:
            raise ValueError(
                f"task '{task.task_id}' business point '{business_point_id}' "
                f"is not mapped by point table '{point_table.point_table_id}' "
                f"for device '{device_id}'"
            )
        point_ids.extend(point.point_id for point in matches)

    return CollectionWork(
        task_id=task.task_id,
        device_id=device_id,
        candidate_connection_ids=connection_ids,
        point_ids=tuple(point_ids),
        target_sink_ids=task.target_sink_ids,
        interval=task.interval,
    )
