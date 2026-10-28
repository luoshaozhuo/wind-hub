"""Modbus Sink 编码器纯单元测试（无 pymodbus、无网络）。

覆盖 bit/register 两类空间、八种数值 datatype、byte/word 双字节序、
bool/int8/uint8 单列编码，以及全部类型/空值错误分支。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.application import Quality
from core.application.sink_contract import (
    ModbusSinkAddress,
    ResolvedSinkPoint,
    SinkSource,
    StreamSinkAddress,
)
from core.application.sink_export import ExportedSinkPointValue
from core.infrastructure.sink.modbus.codec import encode_modbus_value


def _definition(
    *,
    datatype: str = "uint16",
    register_type: str = "holding",
    address: int = 0,
    byte_order: str = "big",
    word_order: str = "big",
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id="wind_speed"),
        ref="scada.wind_speed",
        source_data_type="float32",
        source_unit="meter_per_second",
        datatype=datatype,
        unit="meter_per_second",
        address=ModbusSinkAddress(
            slave_id=1,
            register_type=register_type,
            address=address,
            byte_order=byte_order,
            word_order=word_order,
        ),
    )


def _exported(definition: ResolvedSinkPoint, value) -> ExportedSinkPointValue:
    return ExportedSinkPointValue(
        definition=definition,
        value=value,
        quality=Quality.GOOD,
        timestamp=datetime(2026, 10, 3, tzinfo=UTC),
        source_protocol="modbus",
    )


@pytest.mark.parametrize(
    ("register_type", "value"),
    [("coil", True), ("coil", False), ("discrete", True)],
)
def test_bit_values_encode_single_coil(register_type: str, value: bool) -> None:
    encoded = encode_modbus_value(
        _exported(_definition(datatype="bool", register_type=register_type), value)
    )

    assert encoded.slave_id == 1
    assert encoded.register_type == register_type
    assert encoded.bits == (value,)
    assert encoded.registers == ()


def test_bit_register_rejects_non_bool() -> None:
    with pytest.raises(TypeError, match="requires bool value"):
        encode_modbus_value(_exported(_definition(register_type="coil"), 1))


@pytest.mark.parametrize(
    ("datatype", "value", "expected"),
    [
        ("bool", True, [1]),
        ("bool", False, [0]),
        ("int8", -1, [255]),
        ("uint8", 255, [255]),
        ("int16", -1, [65535]),
        ("uint16", 0x1234, [0x1234]),
    ],
)
def test_single_word_datatypes(datatype: str, value, expected: list[int]) -> None:
    encoded = encode_modbus_value(_exported(_definition(datatype=datatype), value))
    assert encoded.registers == tuple(expected)


def test_int32_big_endian_word_and_byte() -> None:
    encoded = encode_modbus_value(_exported(_definition(datatype="int32"), 0x12345678))
    assert encoded.registers == (0x1234, 0x5678)


def test_int32_word_swap_only() -> None:
    encoded = encode_modbus_value(
        _exported(_definition(datatype="int32", word_order="little"), 0x12345678)
    )
    assert encoded.registers == (0x5678, 0x1234)


def test_int32_byte_swap_only() -> None:
    encoded = encode_modbus_value(
        _exported(_definition(datatype="int32", byte_order="little"), 0x12345678)
    )
    assert encoded.registers == (0x3412, 0x7856)


def test_int32_both_orders_swapped() -> None:
    encoded = encode_modbus_value(
        _exported(
            _definition(datatype="int32", byte_order="little", word_order="little"),
            0x12345678,
        )
    )
    assert encoded.registers == (0x7856, 0x3412)


def test_float32_known_bit_pattern() -> None:
    encoded = encode_modbus_value(_exported(_definition(datatype="float32"), 1.0))
    assert encoded.registers == (0x3F80, 0x0000)


def test_float64_spans_four_words() -> None:
    encoded = encode_modbus_value(_exported(_definition(datatype="float64"), 1.0))
    assert len(encoded.registers) == 4
    assert encoded.registers[:2] == (0x3FF0, 0x0000)


def test_int64_spans_four_words() -> None:
    encoded = encode_modbus_value(_exported(_definition(datatype="int64"), 0x1122334455667788))
    assert len(encoded.registers) == 4
    assert encoded.registers == (0x1122, 0x3344, 0x5566, 0x7788)


def test_unknown_datatype_is_type_error() -> None:
    # "str" 是合法 DataType 但 Modbus 无寄存器编码：编码器必须显式拒绝。
    with pytest.raises(TypeError, match="Unsupported Modbus datatype"):
        encode_modbus_value(_exported(_definition(datatype="str"), "x"))


def test_non_numeric_register_value_is_type_error() -> None:
    with pytest.raises(TypeError, match="requires numeric value"):
        encode_modbus_value(_exported(_definition(datatype="uint16"), "12"))


def test_bool_is_not_accepted_as_numeric() -> None:
    with pytest.raises(TypeError, match="requires numeric value"):
        encode_modbus_value(_exported(_definition(datatype="uint16"), True))


def test_none_value_is_value_error() -> None:
    with pytest.raises(ValueError, match="value is None"):
        encode_modbus_value(_exported(_definition(), None))


def test_non_modbus_address_is_type_error() -> None:
    definition = ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id="wind_speed"),
        ref="scada.wind_speed",
        source_data_type="float32",
        source_unit="meter_per_second",
        datatype="float32",
        unit="meter_per_second",
        address=StreamSinkAddress(field="wind_speed"),
    )
    with pytest.raises(TypeError, match="does not use ModbusSinkAddress"):
        encode_modbus_value(_exported(definition, 1.0))
