"""Core Modbus Sink 点映射边界测试。"""

from dataclasses import FrozenInstanceError

import pytest

from core.application.sink_mapping import (
    ModbusAddress,
    SinkPointMapping,
    SinkSource,
    validate_modbus_mappings,
)


def mapping(name: str, start: int, data_type: str = "float32") -> SinkPointMapping:
    return SinkPointMapping(
        source=SinkSource(device_id="WT001", point_id=name),
        address=ModbusAddress(
            unit_id=1, register_type="holding", address=start, data_type=data_type
        ),
    )


def test_adjacent_ranges_are_allowed() -> None:
    validate_modbus_mappings([mapping("power", 0), mapping("speed", 2)])


def test_overlapping_multiregister_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="overlapping"):
        validate_modbus_mappings([mapping("power", 0), mapping("speed", 1)])


def test_duplicate_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate sink source"):
        validate_modbus_mappings([mapping("power", 0), mapping("power", 10)])


@pytest.mark.parametrize(
    ("register_type", "data_type"),
    [("coil", "float32"), ("discrete", "int16")],
)
def test_bit_table_rejects_numeric_values(register_type: str, data_type: str) -> None:
    with pytest.raises(ValueError, match="bit tables"):
        ModbusAddress(
            unit_id=1, register_type=register_type, address=0, data_type=data_type
        )


def test_address_end_of_table_is_checked() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        mapping("power", 65535)


def test_scale_must_be_finite_and_nonzero() -> None:
    with pytest.raises(ValueError, match="scale"):
        SinkPointMapping(
            source=SinkSource("WT001", "speed"),
            address=ModbusAddress(1, "holding", 0, "float32"),
            scale=float("nan"),
        )


def test_mapping_is_immutable() -> None:
    value = mapping("power", 0)
    with pytest.raises(FrozenInstanceError):
        value.scale = 2.0  # type: ignore[misc]
