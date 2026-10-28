"""Collector 只读批量测点配置查询；不发起设备或 Sink I/O。"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain import DeviceGroupId, DeviceId, Task

from .config_service import CollectorConfigService


@dataclass(frozen=True, slots=True)
class PointMetadata:
    point_id: str
    access: str
    data_type: str
    unit: str
    scale: float
    offset: float
    configured_address: dict[str, str | int | float | bool]
    task_ids: tuple[str, ...]
    sink_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PointMetadataResult:
    point_id: str
    metadata: PointMetadata | None


@dataclass(frozen=True, slots=True)
class PointsMetadataResult:
    device_id: str
    active_revision: str
    active_config_hash: str
    points: tuple[PointMetadataResult, ...]


class CollectorMetadataService:
    """从已提交的 Collector 配置快照查询点表和已启用的采集路由。"""

    def __init__(self, config_service: CollectorConfigService) -> None:
        self._config_service = config_service

    def get_points_metadata(self, device_id: str, point_ids: list[str]) -> PointsMetadataResult:
        if not device_id:
            raise ValueError("device_id must not be empty")
        if not point_ids or any(not item for item in point_ids):
            raise ValueError("point_ids must contain non-empty values")

        # snapshot() 在 activate 同步提交段内读取，同一次调用不跨版本。
        config, revision, config_hash = self._config_service.active_snapshot()
        device_key = DeviceId(device_id)
        device = config.devices.get(device_key)
        if device is None:
            if device_key in config.disabled_devices:
                raise KeyError(f"device '{device_id}' is disabled")
            raise KeyError(f"unknown device '{device_id}'")
        point_table = config.point_table_for_device(device.device_id)
        # 一次请求只索引一次启用 Task：按 (device_group, point_group) 归组，
        # 避免逐点全量扫描 Task 注册表。
        tasks_by_group: dict[tuple[DeviceGroupId, str], list[Task]] = {}
        for task in config.tasks.values():
            if task.enabled:
                tasks_by_group.setdefault((task.device_group_id, task.point_group), []).append(task)
        rows: list[PointMetadataResult] = []
        for point_id in point_ids:
            point = point_table.points.get(point_id)
            if point is None:
                rows.append(PointMetadataResult(point_id=point_id, metadata=None))
                continue
            tasks = tuple(
                task
                for group_id in device.device_group_ids
                for point_group in point.point_groups
                for task in tasks_by_group.get((group_id, point_group), ())
            )
            task_ids = tuple(sorted(task.task_id for task in tasks))
            sink_ids: tuple[str, ...] = ()
            # data_type 由配置解析器写入 ext（与 BusinessPoint 校验一致），
            # 是权威来源；不回退到协议地址里的 "type"（如 IEC104 ASDU 类型）。
            raw_type = point.ext.get("data_type")
            rows.append(
                PointMetadataResult(
                    point_id=point_id,
                    metadata=PointMetadata(
                        point_id=point_id,
                        access=point.access.value,
                        data_type=str(raw_type) if raw_type is not None else "",
                        unit=point.source_unit.unit_id,
                        scale=point.scale,
                        offset=point.offset,
                        configured_address={
                            key: value
                            for key, value in point.ext.items()
                            if value is not None and key != "data_type"
                        },
                        task_ids=task_ids,
                        sink_ids=sink_ids,
                    ),
                )
            )
        return PointsMetadataResult(
            device_id=device_id,
            active_revision=revision,
            active_config_hash=config_hash,
            points=tuple(rows),
        )
