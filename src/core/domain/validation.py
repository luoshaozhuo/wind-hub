"""Shared Domain 配置一致性规则。"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from .device import Device, DeviceGroup, DeviceModel, DeviceType, ProtocolOptions
from .identities import (
    BusinessPointId,
    DeviceGroupId,
    DeviceId,
    DeviceModelId,
    DeviceTypeId,
    PointTableId,
)
from .point import BusinessPoint, PointTable
from .sink import Sink
from .task import Task, validate_task_references
from .unit import UNIT_CATALOG, Quantity, Unit
from .value_objects import DataType


def validate_core_config(
    *,
    device_types: Mapping[DeviceTypeId, DeviceType],
    device_models: Mapping[DeviceModelId, DeviceModel],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
    devices: Mapping[DeviceId, Device],
    business_points: Mapping[BusinessPointId, BusinessPoint],
    point_tables: Mapping[PointTableId, PointTable],
    protocol_options_by_device: Mapping[DeviceId, ProtocolOptions],
    tasks: Mapping[str, Task] | None = None,
    sink_ids: Collection[str] = (),
    sinks: Mapping[str, Sink] | None = None,
) -> None:
    """校验配置索引的领域引用、键-身份一致性与跨对象不变量。"""
    _validate_identity(device_types, "device_types", "device_type_id")
    _validate_identity(device_models, "device_models", "device_model_id")
    _validate_identity(device_groups, "device_groups", "device_group_id")
    _validate_identity(devices, "devices", "device_id")
    _validate_identity(business_points, "business_points", "business_point_id")
    _validate_identity(point_tables, "point_tables", "point_table_id")
    _validate_business_points(business_points)
    _validate_point_table_parents(point_tables)
    _validate_point_tables(point_tables, business_points)
    _validate_device_models(device_models, device_types, point_tables)
    _validate_devices(devices, device_models, device_groups)
    _validate_device_options(protocol_options_by_device, devices)
    if sinks is not None:
        _validate_sinks(sinks, devices, device_models, point_tables)
    if tasks is not None:
        validate_task_references(tasks, device_groups, sink_ids)


def _validate_identity(
    values: Mapping[Any, Any],
    index_name: str,
    identity_attr: str,
) -> None:
    for key, value in values.items():
        identity = getattr(value, identity_attr)
        if key != identity:
            raise ValueError(
                f"{index_name} key '{key}' does not match " f"{identity_attr} '{identity}'"
            )


def _validate_business_points(
    business_points: Mapping[BusinessPointId, BusinessPoint],
) -> None:
    for point in business_points.values():
        _validate_canonical_unit(
            point.standard_unit,
            context=f"business point '{point.business_point_id}' standard_unit",
        )
        if (
            point.data_type in (DataType.BOOL, DataType.STRING)
            and point.standard_unit.quantity is not Quantity.DIMENSIONLESS
        ):
            raise ValueError(
                f"business point '{point.business_point_id}' with "
                f"{point.data_type.value} value must use dimensionless unit"
            )


def _validate_point_table_parents(
    tables: Mapping[PointTableId, PointTable],
) -> None:
    """检查父表存在、协议一致和继承关系无环。"""
    visited: set[PointTableId] = set()
    visiting: set[PointTableId] = set()

    def visit(table_id: PointTableId) -> None:
        if table_id in visiting:
            raise ValueError(f"point table inheritance cycle at '{table_id}'")
        if table_id in visited:
            return
        visiting.add(table_id)
        table = tables[table_id]
        if table.parent_id is not None:
            parent = tables.get(table.parent_id)
            if parent is None:
                raise ValueError(
                    f"point table '{table_id}' references unknown parent '{table.parent_id}'"
                )
            if table.protocol != parent.protocol:
                raise ValueError(
                    f"point table '{table_id}' protocol differs from parent '{table.parent_id}'"
                )
            visit(table.parent_id)
        visiting.remove(table_id)
        visited.add(table_id)

    for table_id in tables:
        visit(table_id)


def _validate_point_tables(
    point_tables: Mapping[PointTableId, PointTable],
    business_points: Mapping[BusinessPointId, BusinessPoint],
) -> None:
    for table in point_tables.values():
        for point in table.points.values():
            _validate_canonical_unit(
                point.source_unit,
                context=(
                    f"point table '{table.point_table_id}' " f"point '{point.point_id}' source_unit"
                ),
            )
            business_point = business_points.get(point.business_point_id)
            if business_point is None:
                raise ValueError(
                    f"point table '{table.point_table_id}' point "
                    f"'{point.point_id}' references unknown business point "
                    f"'{point.business_point_id}'"
                )
            if point.source_unit.quantity != business_point.standard_unit.quantity:
                raise ValueError(
                    f"point table '{table.point_table_id}' point "
                    f"'{point.point_id}' unit quantity does not match business "
                    f"point '{point.business_point_id}'"
                )
            if business_point.data_type in (DataType.BOOL, DataType.STRING) and (
                point.scale != 1.0 or point.offset != 0.0
            ):
                raise ValueError(
                    f"point table '{table.point_table_id}' point "
                    f"'{point.point_id}' with {business_point.data_type.value} "
                    "value must use identity scale/offset"
                )


def _validate_device_models(
    device_models: Mapping[DeviceModelId, DeviceModel],
    device_types: Mapping[DeviceTypeId, DeviceType],
    point_tables: Mapping[PointTableId, PointTable],
) -> None:
    for model in device_models.values():
        if model.device_type_id not in device_types:
            raise ValueError(
                f"device model '{model.device_model_id}' references unknown "
                f"device type '{model.device_type_id}'"
            )
        if model.point_table_id not in point_tables:
            raise ValueError(
                f"device model '{model.device_model_id}' references unknown "
                f"point table '{model.point_table_id}'"
            )


def _validate_devices(
    devices: Mapping[DeviceId, Device],
    device_models: Mapping[DeviceModelId, DeviceModel],
    device_groups: Mapping[DeviceGroupId, DeviceGroup],
) -> None:
    for device in devices.values():
        if device.device_model_id not in device_models:
            raise ValueError(
                f"device '{device.device_id}' references unknown model "
                f"'{device.device_model_id}'"
            )
        unknown_groups = set(device.device_group_ids) - set(device_groups)
        if unknown_groups:
            raise ValueError(
                f"device '{device.device_id}' references unknown groups "
                f"{sorted(str(value) for value in unknown_groups)}"
            )


def _validate_device_options(
    protocol_options_by_device: Mapping[DeviceId, ProtocolOptions],
    devices: Mapping[DeviceId, Device],
) -> None:
    unknown_devices = set(protocol_options_by_device) - set(devices)
    if unknown_devices:
        raise ValueError(
            "device options reference unknown devices: "
            f"{sorted(str(value) for value in unknown_devices)}"
        )


def _validate_canonical_unit(unit: Unit, *, context: str) -> None:
    canonical = UNIT_CATALOG.get(unit.code)
    if canonical is None or unit != canonical:
        raise ValueError(f"{context} must use canonical built-in unit '{unit.code.value}'")


def _validate_sinks(
    sinks: Mapping[str, Sink],
    devices: Mapping[DeviceId, Device],
    models: Mapping[DeviceModelId, DeviceModel],
    tables: Mapping[PointTableId, PointTable],
) -> None:
    """检查 Sink 的稳定引用；连接与地址类型由 YAML Adapter 校验。"""
    _validate_identity(sinks, "sinks", "sink_id")
    for sink_id, sink in sinks.items():
        for point in sink.points:
            source = point.get("source")
            if not isinstance(source, Mapping):
                raise ValueError(f"sink '{sink_id}' point requires a source mapping")
            device_value = source.get("device_id")
            point_id = source.get("point_id")
            if not isinstance(device_value, str) or not device_value:
                raise ValueError(f"sink '{sink_id}' invalid source device_id")
            device_id = DeviceId(device_value)
            device = devices.get(device_id)
            if device is None:
                raise ValueError(f"sink '{sink_id}' unknown device '{device_id}'")
            table = tables[models[device.device_model_id].point_table_id]
            if point_id not in table.points:
                raise ValueError(f"sink '{sink_id}' unknown point '{point_id}' on '{device_id}'")
