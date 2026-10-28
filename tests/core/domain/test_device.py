"""设备领域聚合根的独立不变量测试（device.py）。

配置链路只构造合法对象，这里直接覆盖所有拒绝分支与快照方法：
类型/分组去空白与空值、型号属性冻结/有限性、设备分组幂等与末组保护。
"""

from __future__ import annotations

import math
from types import MappingProxyType

import pytest

from core.domain.device import (
    Device,
    DeviceGroup,
    DeviceModel,
    DeviceType,
    freeze_protocol_options,
)
from core.domain.value_objects import ConnectionEndpoint


def _endpoint(host: str = "127.0.0.1", port: int | None = 502) -> ConnectionEndpoint:
    return ConnectionEndpoint(host=host, port=port)


# --------------------------------------------------------------------------
# DeviceType / DeviceGroup
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("factory", "kwargs"),
    [
        (DeviceType, {"device_type_id": "t", "name": "n"}),
        (DeviceGroup, {"device_group_id": "g", "name": "n"}),
    ],
)
def test_identity_and_name_trimmed(factory, kwargs) -> None:
    obj = factory(**{key: f"  {value}  " for key, value in kwargs.items()})
    assert getattr(obj, list(kwargs)[0]) == list(kwargs.values())[0]
    assert obj.name == "n"


@pytest.mark.parametrize(
    ("factory", "identity_field"),
    [(DeviceType, "device_type_id"), (DeviceGroup, "device_group_id")],
)
def test_blank_identity_and_name_rejected(factory, identity_field) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        factory(**{identity_field: "  ", "name": "n"})
    with pytest.raises(ValueError, match="name must not be empty"):
        factory(**{identity_field: "x", "name": "  "})


# --------------------------------------------------------------------------
# DeviceModel
# --------------------------------------------------------------------------


def _model(**overrides) -> DeviceModel:
    values = {
        "device_model_id": "modbus_wtg",
        "device_type_id": "wtg",
        "point_table_id": "wtg_points",
        "name": "WTG",
        "properties": {},
        "endpoint_defaults": {},
        "protocol_options": {},
    }
    values.update(overrides)
    return DeviceModel(**values)


@pytest.mark.parametrize("field_name", ["device_model_id", "device_type_id", "point_table_id"])
def test_model_blank_ids_rejected(field_name: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _model(**{field_name: "  "})


def test_model_rejects_unknown_endpoint_default() -> None:
    with pytest.raises(ValueError, match="only supports 'port'"):
        _model(endpoint_defaults={"host": "1.2.3.4"})


def test_model_properties_deep_frozen() -> None:
    model = _model(properties={"limits": {"min": 0}, "tags": ["a"]})
    assert isinstance(model.properties, MappingProxyType)
    assert isinstance(model.properties["limits"], MappingProxyType)
    assert model.properties["tags"] == ("a",)


def test_model_rejects_non_finite_float_property() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        _model(properties={"rate": math.nan})


def test_model_rejects_unsupported_property_type() -> None:
    with pytest.raises(ValueError, match="invalid device model property type"):
        _model(properties={"object": object()})


def test_model_protocol_options_frozen_and_validated() -> None:
    model = _model(protocol_options={"unit_id": 1})
    assert isinstance(model.protocol_options, MappingProxyType)
    assert model.protocol_options["unit_id"] == 1


# --------------------------------------------------------------------------
# Device
# --------------------------------------------------------------------------


def _device(**overrides) -> Device:
    values = {
        "device_id": "wtg-001",
        "device_model_id": "modbus_wtg",
        "endpoint": _endpoint(),
        "device_group_ids": ("group-a",),
    }
    values.update(overrides)
    return Device(**values)


def test_device_identity_trimmed_and_name_blank_normalized() -> None:
    device = _device(device_id="  wtg-002  ", name="  ")
    assert device.device_id == "wtg-002"
    assert device.name is None


@pytest.mark.parametrize("field_name", ["device_id", "device_model_id"])
def test_device_blank_ids_rejected(field_name: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _device(**{field_name: " "})


def test_device_requires_at_least_one_group() -> None:
    with pytest.raises(ValueError, match="at least one device group"):
        _device(device_group_ids=())


def test_device_rejects_empty_or_duplicate_groups() -> None:
    with pytest.raises(ValueError, match="empty values"):
        _device(device_group_ids=("  ",))
    with pytest.raises(ValueError, match="duplicates"):
        _device(device_group_ids=("g", "g"))


def test_device_rejects_non_bool_flags() -> None:
    with pytest.raises(ValueError, match="enabled must be bool"):
        _device(enabled=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="subscribe_enabled must be bool"):
        _device(subscribe_enabled="yes")  # type: ignore[arg-type]


def test_rename_and_change_model_return_new_snapshots() -> None:
    device = _device()
    renamed = device.rename("新名字")
    assert renamed is not device and renamed.name == "新名字"
    changed = device.change_model("other_model")
    assert changed.device_model_id == "other_model"
    assert device.device_model_id == "modbus_wtg"  # 原快照不变


def test_assign_group_is_idempotent_and_appends() -> None:
    device = _device()
    assert device.assign_group("group-a") is device
    moved = device.assign_group(" group-b ")
    assert moved.device_group_ids == ("group-a", "group-b")
    with pytest.raises(ValueError, match="must not be empty"):
        device.assign_group("  ")


def test_remove_group_is_idempotent_but_keeps_last() -> None:
    device = _device(device_group_ids=("a", "b"))
    assert device.remove_group("missing") is device
    assert device.remove_group("a").device_group_ids == ("b",)
    with pytest.raises(ValueError, match="last device group"):
        _device(device_group_ids=("only",)).remove_group("only")
    with pytest.raises(ValueError, match="must not be empty"):
        device.remove_group("  ")


def test_freeze_protocol_options_rejects_blank_key_and_non_finite() -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        freeze_protocol_options({"  ": 1})
    with pytest.raises(ValueError, match="must be finite"):
        freeze_protocol_options({"rate": math.inf})
