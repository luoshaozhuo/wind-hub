"""Modbus Sink 编码单元测试。"""

from datetime import UTC, datetime

import pytest

from wind_hub_collector.adapter.outbound.sink.modbus_codec import encode_modbus_value
from wind_hub_collector.application.sink_export import ExportedSinkPointValue
from wind_hub_core.config import ModbusSinkAddress, ResolvedSinkPoint, SinkSource
from wind_hub_core.model.point import Quality


def _value(
    raw: object,
    *,
    datatype: str,
    register_type: str = "holding",
    byte_order: str = "big",
    word_order: str = "big",
) -> ExportedSinkPointValue:
    point = ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id="p1"),
        ref="wt01.p1",
        source_data_type=datatype,
        source_unit="none",
        datatype=datatype,
        unit="none",
        address=ModbusSinkAddress(
            unit_id=1,
            register_type=register_type,  # type: ignore[arg-type]
            address=100,
            byte_order=byte_order,  # type: ignore[arg-type]
            word_order=word_order,  # type: ignore[arg-type]
        ),
    )
    return ExportedSinkPointValue(
        definition=point,
        value=raw,
        quality=Quality.GOOD,
        timestamp=datetime(2026, 10, 4, tzinfo=UTC),
        source_protocol="modbus",
    )


def test_encode_coil_bool() -> None:
    encoded = encode_modbus_value(_value(True, datatype="bool", register_type="coil"))
    assert encoded.bits == (True,)
    assert encoded.registers == ()
    assert encoded.unit_id == 1
    assert encoded.address == 100


def test_encode_uint16_big_endian() -> None:
    encoded = encode_modbus_value(_value(0x1234, datatype="uint16"))
    assert encoded.registers == (0x1234,)


def test_encode_int16_little_byte_order() -> None:
    encoded = encode_modbus_value(_value(-2, datatype="int16", byte_order="little"))
    assert encoded.registers == (0xFEFF,)


def test_encode_float32_big_big() -> None:
    encoded = encode_modbus_value(_value(1.0, datatype="float32"))
    assert encoded.registers == (0x3F80, 0x0000)


def test_encode_float32_little_word_order() -> None:
    encoded = encode_modbus_value(_value(1.0, datatype="float32", word_order="little"))
    assert encoded.registers == (0x0000, 0x3F80)


def test_encode_float32_little_byte_and_word_order() -> None:
    encoded = encode_modbus_value(
        _value(
            1.0,
            datatype="float32",
            byte_order="little",
            word_order="little",
        )
    )
    assert encoded.registers == (0x0000, 0x803F)


def test_encode_none_rejected() -> None:
    with pytest.raises(ValueError, match="value is None"):
        encode_modbus_value(_value(None, datatype="float32"))


def test_encode_non_bool_bit_rejected() -> None:
    with pytest.raises(TypeError, match="requires bool value"):
        encode_modbus_value(_value(1, datatype="bool", register_type="discrete"))


def test_encode_str_register_rejected() -> None:
    with pytest.raises(TypeError, match="Unsupported Modbus datatype"):
        encode_modbus_value(_value("abc", datatype="str"))
