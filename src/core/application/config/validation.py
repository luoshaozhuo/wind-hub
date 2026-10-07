"""Shared Core 配置一致性校验。"""

from __future__ import annotations

from core.domain import Quantity, ValueType

from .snapshot import CoreConfigSnapshot


def validate_core_config(snapshot: CoreConfigSnapshot) -> None:
    """校验 Shared Core 配置中的跨对象引用与语义约束。"""
    _validate_business_points(snapshot)
    _validate_device_models(snapshot)
    _validate_devices(snapshot)
    _validate_point_tables(snapshot)
    _validate_device_connections(snapshot)


def _validate_business_points(snapshot: CoreConfigSnapshot) -> None:
    for point in snapshot.business_points.values():
        if (
            point.value_type in (ValueType.BOOLEAN, ValueType.STRING)
            and point.standard_unit.quantity is not Quantity.DIMENSIONLESS
        ):
            raise ValueError(
                f"business point '{point.business_point_id}' with value type "
                f"'{point.value_type}' must use a dimensionless standard unit"
            )


def _validate_device_models(snapshot: CoreConfigSnapshot) -> None:
    for model in snapshot.device_models.values():
        if model.device_type_id not in snapshot.device_types:
            raise ValueError(
                f"device model '{model.device_model_id}' references unknown "
                f"device type '{model.device_type_id}'"
            )
        if model.point_table_id not in snapshot.point_tables:
            raise ValueError(
                f"device model '{model.device_model_id}' references unknown "
                f"point table '{model.point_table_id}'"
            )


def _validate_devices(snapshot: CoreConfigSnapshot) -> None:
    for device in snapshot.devices.values():
        if device.device_model_id not in snapshot.device_models:
            raise ValueError(
                f"device '{device.device_id}' references unknown device model "
                f"'{device.device_model_id}'"
            )
        for group_id in device.device_group_ids:
            if group_id not in snapshot.device_groups:
                raise ValueError(
                    f"device '{device.device_id}' references unknown device group "
                    f"'{group_id}'"
                )


def _validate_point_tables(snapshot: CoreConfigSnapshot) -> None:
    for table in snapshot.point_tables.values():
        for point in table.points.values():
            business_point = snapshot.business_points.get(point.business_point_id)
            if business_point is None:
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"references unknown business point '{point.business_point_id}'"
                )
            if point.source_unit.quantity != business_point.standard_unit.quantity:
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"uses source quantity '{point.source_unit.quantity}', but "
                    f"business point '{business_point.business_point_id}' expects "
                    f"'{business_point.standard_unit.quantity}'"
                )
            if (
                business_point.value_type in (ValueType.BOOLEAN, ValueType.STRING)
                and (point.scale != 1.0 or point.offset != 0.0)
            ):
                raise ValueError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"maps {business_point.value_type} with non-identity scale/offset"
                )


def _validate_device_connections(snapshot: CoreConfigSnapshot) -> None:
    for connection in snapshot.device_connections.values():
        if connection.device_id not in snapshot.devices:
            raise ValueError(
                f"connection '{connection.connection_id}' references unknown device "
                f"'{connection.device_id}'"
            )
