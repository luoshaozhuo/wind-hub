"""Shared Domain 配置一致性规则。"""

from __future__ import annotations

from collections.abc import Mapping
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
) -> None:
    """校验配置索引的领域引用、键-身份一致性与跨对象不变量。"""
    _validate_identity(device_types, "device_types", "device_type_id")
    _validate_identity(device_models, "device_models", "device_model_id")
    _validate_identity(device_groups, "device_groups", "device_group_id")
    _validate_identity(devices, "devices", "device_id")
    _validate_identity(business_points, "business_points", "business_point_id")
    _validate_identity(point_tables, "point_tables", "point_table_id")
    _validate_business_points(business_points)
    _validate_point_tables(point_tables, business_points)
    _validate_device_models(device_models, device_types, point_tables)
    _validate_devices(devices, device_models, device_groups)
    _validate_device_options(protocol_options_by_device, devices)


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
