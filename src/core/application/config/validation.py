"""Shared Core 配置一致性校验。"""

from __future__ import annotations

from core.domain import DataType, UNIT_CATALOG, Quantity, Unit

from ..errors import ConfigError
from .snapshot import CoreConfigSnapshot


def validate_core_config(snapshot: CoreConfigSnapshot) -> None:
    """校验 Shared Core 静态配置引用与跨对象不变量。"""
    _validate_business_points(snapshot)
    _validate_point_tables(snapshot)
    _validate_device_models(snapshot)
    _validate_devices(snapshot)
    _validate_protocol_options(snapshot)


def _validate_business_points(snapshot: CoreConfigSnapshot) -> None:
    for point in snapshot.business_points.values():
        _validate_canonical_unit(
            point.standard_unit,
            context=f"business point '{point.business_point_id}' standard_unit",
        )
        if (
            point.data_type in (DataType.BOOL, DataType.STRING)
            and point.standard_unit.quantity is not Quantity.DIMENSIONLESS
        ):
            raise ConfigError(
                f"business point '{point.business_point_id}' with "
                f"{point.data_type.value} value must use dimensionless unit"
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
            business_point = snapshot.business_points.get(
                point.business_point_id
            )
            if business_point is None:
                raise ConfigError(
                    f"point table '{table.point_table_id}' point "
                    f"'{point.point_id}' references unknown business point "
                    f"'{point.business_point_id}'"
                )
            if point.source_unit.quantity != business_point.standard_unit.quantity:
                raise ConfigError(
                    f"point table '{table.point_table_id}' point "
                    f"'{point.point_id}' unit quantity does not match business "
                    f"point '{point.business_point_id}'"
                )
            if (
                business_point.data_type in (DataType.BOOL, DataType.STRING)
                and (point.scale != 1.0 or point.offset != 0.0)
            ):
                raise ConfigError(
                    f"point table '{table.point_table_id}' point "
                    f"'{point.point_id}' with {business_point.data_type.value} "
                    "value must use identity scale/offset"
                )


def _validate_device_models(snapshot: CoreConfigSnapshot) -> None:
    for model in snapshot.device_models.values():
        if model.device_type_id not in snapshot.device_types:
            raise ConfigError(
                f"device model '{model.device_model_id}' references unknown "
                f"device type '{model.device_type_id}'"
            )
        if model.point_table_id not in snapshot.point_tables:
            raise ConfigError(
                f"device model '{model.device_model_id}' references unknown "
                f"point table '{model.point_table_id}'"
            )


def _validate_devices(snapshot: CoreConfigSnapshot) -> None:
    for device in snapshot.devices.values():
        if device.device_model_id not in snapshot.device_models:
            raise ConfigError(
                f"device '{device.device_id}' references unknown model "
                f"'{device.device_model_id}'"
            )
        unknown_groups = set(device.device_group_ids) - set(snapshot.device_groups)
        if unknown_groups:
            raise ConfigError(
                f"device '{device.device_id}' references unknown groups "
                f"{sorted(str(value) for value in unknown_groups)}"
            )


def _validate_protocol_options(snapshot: CoreConfigSnapshot) -> None:
    unknown_devices = set(snapshot.device_options) - set(snapshot.devices)
    if unknown_devices:
        raise ConfigError(
            "device options reference unknown devices: "
            f"{sorted(str(value) for value in unknown_devices)}"
        )



def _validate_canonical_unit(unit: Unit, *, context: str) -> None:
    canonical = UNIT_CATALOG.get(unit.code)
    if canonical is None or unit != canonical:
        raise ConfigError(
            f"{context} must use canonical built-in unit '{unit.code.value}'"
        )
