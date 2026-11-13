"""Web configuration rules extending Core shared-domain validation."""

from __future__ import annotations

from collections.abc import Mapping

from core.configuration.validation import validate_core_config as validate_shared_config
from core.domain.device import Device, DeviceGroup, DeviceModel, DeviceType
from core.domain.identities import (
    BusinessPointId,
    DeviceGroupId,
    DeviceId,
    DeviceModelId,
    DeviceTypeId,
    PointTableId,
)
from core.domain.point import BusinessPoint, PointTable
from core.domain.unit import UnitCatalog

from .sink import Sink
from .task import Task, validate_task_references


def validate_core_config(
    *,
    units: UnitCatalog,
    device_types: Mapping[DeviceTypeId, DeviceType],
    device_models: Mapping[DeviceModelId, DeviceModel],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
    devices: Mapping[DeviceId, Device],
    business_points: Mapping[BusinessPointId, BusinessPoint],
    point_tables: Mapping[PointTableId, PointTable],
    tasks: Mapping[str, Task] | None = None,
    sinks: Mapping[str, Sink] | None = None,
) -> None:
    """Validate shared entities and optional Web-managed legacy collections."""
    validate_shared_config(
        units=units,
        device_types=device_types,
        device_models=device_models,
        device_groups=device_groups,
        devices=devices,
        business_points=business_points,
        point_tables=point_tables,
    )
    if sinks is not None:
        _validate_sinks(sinks)
    if tasks is not None:
        validate_task_references(tasks, device_groups)
        _validate_task_point_groups(tasks, devices, device_models, point_tables)


def _validate_sinks(sinks: Mapping[str, Sink]) -> None:
    """Validate configuration identity independently of acquisition Tasks."""
    for key, sink in sinks.items():
        if key != sink.sink_id:
            raise ValueError(
                f"sinks key '{key}' does not match sink_id '{sink.sink_id}'"
            )


def _validate_task_point_groups(
    tasks: Mapping[str, Task],
    devices: Mapping[DeviceId, Device],
    models: Mapping[DeviceModelId, DeviceModel],
    tables: Mapping[PointTableId, PointTable],
) -> None:
    """确保每台参与任务的启用设备至少有一个可采集测点。"""
    for task in tasks.values():
        if not task.enabled:
            continue
        for device in devices.values():
            if not device.enabled or task.device_group_id not in device.device_group_ids:
                continue
            table = tables[models[device.device_model_id].point_table_id]
            if not any(task.point_group in point.point_groups for point in table.points.values()):
                raise ValueError(
                    f"task '{task.task_id}' point group '{task.point_group}' "
                    f"is absent on device '{device.device_id}'"
                )
