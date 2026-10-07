"""Shared Core 配置一致性校验。"""

from __future__ import annotations

from core.domain import UNIT_CATALOG, Quantity, Unit, ValueType

from ..errors import ConfigError
from .snapshot import CoreConfigSnapshot


def validate_core_config(snapshot: CoreConfigSnapshot) -> None:
    """校验 Shared Core 配置中的跨对象引用与语义约束。"""
    _validate_business_points(snapshot)
    _validate_device_models(snapshot)
    _validate_device_model_point_tables(snapshot)
    _validate_devices(snapshot)
    _validate_point_tables(snapshot)
    _validate_device_connections(snapshot)


def _validate_business_points(snapshot: CoreConfigSnapshot) -> None:
    for point in snapshot.business_points.values():
        _validate_canonical_unit(
            point.standard_unit,
            context=f"business point '{point.business_point_id}' standard_unit",
        )
        if (
            point.value_type in (ValueType.BOOLEAN, ValueType.STRING)
            and point.standard_unit.quantity is not Quantity.DIMENSIONLESS
        ):
            raise ConfigError(
                f"business point '{point.business_point_id}' with value type "
                f"'{point.value_type}' must use a dimensionless standard unit"
            )


def _validate_device_models(snapshot: CoreConfigSnapshot) -> None:
    for model in snapshot.device_models.values():
        if model.device_type_id not in snapshot.device_types:
            raise ConfigError(
                f"device model '{model.device_model_id}' references unknown "
                f"device type '{model.device_type_id}'"
            )


def _validate_device_model_point_tables(
    snapshot: CoreConfigSnapshot,
) -> None:
    model_ids = set(snapshot.device_models)
    binding_ids = set(snapshot.device_model_point_tables)

    missing = model_ids - binding_ids
    if missing:
        raise ConfigError(
            "device models missing point table mapping: "
            f"{sorted(str(value) for value in missing)}"
        )

    unknown = binding_ids - model_ids
    if unknown:
        raise ConfigError(
            "point table mappings reference unknown device models: "
            f"{sorted(str(value) for value in unknown)}"
        )

    for model_id, point_table_id in snapshot.device_model_point_tables.items():
        if point_table_id not in snapshot.point_tables:
            raise ConfigError(
                f"device model '{model_id}' references unknown "
                f"point table '{point_table_id}'"
            )


def _validate_devices(snapshot: CoreConfigSnapshot) -> None:
    for device in snapshot.devices.values():
        if device.device_model_id not in snapshot.device_models:
            raise ConfigError(
                f"device '{device.device_id}' references unknown device model "
                f"'{device.device_model_id}'"
            )
        for group_id in device.device_group_ids:
            if group_id not in snapshot.device_groups:
                raise ConfigError(
                    f"device '{device.device_id}' references unknown device group "
                    f"'{group_id}'"
                )


def _validate_point_tables(snapshot: CoreConfigSnapshot) -> None:
    for table in snapshot.point_tables.values():
        for point in table.points.values():
            _validate_canonical_unit(
                point.source_unit,
                context=(
                    f"point table '{table.point_table_id}' "
                    f"point '{point.point_id}' source_unit"
                ),
            )
            business_point = snapshot.business_points.get(point.business_point_id)
            if business_point is None:
                raise ConfigError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"references unknown business point '{point.business_point_id}'"
                )
            if point.source_unit.quantity != business_point.standard_unit.quantity:
                raise ConfigError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"uses source quantity '{point.source_unit.quantity}', but "
                    f"business point '{business_point.business_point_id}' expects "
                    f"'{business_point.standard_unit.quantity}'"
                )
            if (
                business_point.value_type in (ValueType.BOOLEAN, ValueType.STRING)
                and (point.scale != 1.0 or point.offset != 0.0)
            ):
                raise ConfigError(
                    f"point table '{table.point_table_id}' point '{point.point_id}' "
                    f"maps {business_point.value_type} with non-identity scale/offset"
                )


def _validate_device_connections(snapshot: CoreConfigSnapshot) -> None:
    for connection in snapshot.device_connections.values():
        if connection.device_id not in snapshot.devices:
            raise ConfigError(
                f"connection '{connection.connection_id}' references unknown device "
                f"'{connection.device_id}'"
            )



def _validate_canonical_unit(unit: Unit, *, context: str) -> None:
    """确保配置只引用内置目录中的规范 Unit 值对象。"""
    canonical = UNIT_CATALOG.get(unit.code)
    if canonical is None or unit != canonical:
        raise ConfigError(
            f"{context} must use canonical built-in unit '{unit.code.value}'"
        )
