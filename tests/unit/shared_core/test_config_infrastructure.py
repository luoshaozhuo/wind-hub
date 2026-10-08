from __future__ import annotations

from typing import Any

import pytest

from core.domain import (
    UNIT_CATALOG,
    BusinessPoint,
    ConnectionEndpoint,
    DataType,
    Device,
    DeviceModel,
    DeviceType,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    Quantity,
    Unit,
    UnitCode,
    validate_core_config,
)


def _indexes(
    *,
    device_name: str = "WT01",
) -> dict[str, Any]:
    """返回一组可通过 validate_core_config 校验的领域配置索引。"""
    device_type = DeviceType("wind_turbine", "Wind Turbine")
    point = BusinessPoint(
        "active_power",
        DataType.FLOAT32,
        UNIT_CATALOG[UnitCode.KILOWATT],
    )
    protocol_point = Point(
        point_id="p",
        business_point_id=point.business_point_id,
        source_unit=UNIT_CATALOG[UnitCode.WATT],
        access=PointAccess.READ_WRITE,
        ext={
            "register_type": "holding",
            "address": 1,
            "data_type": "float32",
        },
    )
    table = PointTable(
        "wt_modbus",
        Protocol("modbus"),
        {protocol_point.point_id: protocol_point},
    )
    model = DeviceModel(
        "m1",
        device_type.device_type_id,
        table.point_table_id,
    )
    device = Device(
        "wt01",
        model.device_model_id,
        ConnectionEndpoint("10.0.0.1", 502),
        name=device_name,
    )
    return {
        "device_types": {device_type.device_type_id: device_type},
        "device_models": {model.device_model_id: model},
        "device_groups": {},
        "devices": {device.device_id: device},
        "business_points": {point.business_point_id: point},
        "point_tables": {table.point_table_id: table},
        "device_options": {device.device_id: {"unit_id": 1}},
    }


def test_data_type_coerces_standard_business_values() -> None:
    assert DataType.INT16.coerce(12.0) == 12
    assert DataType.FLOAT32.coerce(12) == 12.0
    assert DataType.BOOL.coerce(True) is True

    with pytest.raises(ValueError, match="outside range"):
        DataType.UINT8.coerce(-1)
    with pytest.raises(ValueError, match="integer value"):
        DataType.INT16.coerce(12.5)
    with pytest.raises(TypeError, match="requires bool"):
        DataType.BOOL.coerce(1)


def test_domain_validation_rejects_noncanonical_unit() -> None:
    indexes = _indexes()
    point = next(iter(indexes["business_points"].values()))
    invalid_point = BusinessPoint(
        point.business_point_id,
        point.data_type,
        Unit(
            UnitCode.KILOWATT,
            "bad",
            Quantity.TIME,
            scale_to_base=2.0,
        ),
    )
    invalid = {
        **indexes,
        "business_points": {invalid_point.business_point_id: invalid_point},
    }

    with pytest.raises(ValueError, match="canonical built-in unit"):
        validate_core_config(**invalid)


def test_domain_validation_rejects_unknown_model_point_table() -> None:
    indexes = _indexes()
    model = next(iter(indexes["device_models"].values()))
    invalid_model = DeviceModel(
        model.device_model_id,
        model.device_type_id,
        "missing_table",
    )
    invalid = {
        **indexes,
        "device_models": {invalid_model.device_model_id: invalid_model},
    }

    with pytest.raises(ValueError, match="unknown point table"):
        validate_core_config(**invalid)


def test_domain_validation_rejects_identity_key_mismatch() -> None:
    indexes = _indexes()
    device = next(iter(indexes["devices"].values()))
    invalid = {**indexes, "devices": {"other_key": device}}

    with pytest.raises(ValueError, match="does not match"):
        validate_core_config(**invalid)
