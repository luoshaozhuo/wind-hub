"""ModbusSinkStore 稀疏地址空间测试（纯内存，无 pymodbus）。

- 构造期按 datatype 字宽预建地址，非 Modbus 地址点跳过；
- bit/register 写入与回读；
- 未知 slave / 未声明地址 / 错误空间名 → KeyError/ValueError。
"""

from __future__ import annotations

import pytest

from core.application.sink_contract import (
    ModbusSinkAddress,
    ResolvedSinkPoint,
    SinkSource,
    StreamSinkAddress,
)
from core.infrastructure.sink.modbus.codec import EncodedModbusValue
from core.infrastructure.sink.modbus.store import ModbusSinkStore


def _point(
    *,
    ref: str = "scada.wind_speed",
    slave_id: int = 1,
    register_type: str = "holding",
    address: int = 0,
    datatype: str = "uint16",
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id=ref),
        ref=ref,
        source_data_type="float32",
        source_unit="meter_per_second",
        datatype=datatype,
        unit="meter_per_second",
        address=ModbusSinkAddress(slave_id=slave_id, register_type=register_type, address=address),
    )


def test_store_predeclares_register_widths_and_sorts_slaves() -> None:
    store = ModbusSinkStore(
        [
            _point(ref="a.w", slave_id=2, register_type="holding", address=10, datatype="int32"),
            _point(ref="b.w", slave_id=1, register_type="coil", address=5, datatype="bool"),
            _point(ref="c.w", slave_id=1, register_type="input", address=0, datatype="float64"),
        ]
    )

    assert store.slave_ids == (1, 2)
    # int32 占两个 word，默认初值 0
    assert store.read_registers(2, "holding", 10, count=2) == [0, 0]
    # coil 默认 False
    assert store.read_bits(1, "coil", 5) == [False]
    # float64 占四个 word
    assert store.read_registers(1, "input", 0, count=4) == [0, 0, 0, 0]


def test_store_skips_non_modbus_address_points() -> None:
    stream_point = ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id="p"),
        ref="scada.p",
        source_data_type="float32",
        source_unit="meter_per_second",
        datatype="float32",
        unit="meter_per_second",
        address=StreamSinkAddress(field="p"),
    )
    store = ModbusSinkStore([stream_point])
    assert store.slave_ids == ()


def test_write_and_read_back_registers() -> None:
    store = ModbusSinkStore([_point(address=10, datatype="int32")])
    store.write(
        EncodedModbusValue(
            slave_id=1, register_type="holding", address=10, registers=(0x1234, 0x5678)
        )
    )

    assert store.read_registers(1, "holding", 10, count=2) == [0x1234, 0x5678]


def test_write_and_read_back_bits() -> None:
    store = ModbusSinkStore([_point(register_type="coil", address=7, datatype="bool")])
    store.write(EncodedModbusValue(slave_id=1, register_type="coil", address=7, bits=(True,)))

    assert store.read_bits(1, "coil", 7) == [True]


def test_write_unknown_slave_is_key_error() -> None:
    store = ModbusSinkStore([_point()])
    with pytest.raises(KeyError, match="Unknown Modbus slave_id 9"):
        store.write(
            EncodedModbusValue(slave_id=9, register_type="holding", address=0, registers=(1,))
        )


def test_write_undeclared_address_is_key_error() -> None:
    store = ModbusSinkStore([_point(address=10, datatype="uint16")])
    with pytest.raises(KeyError, match="Undeclared Modbus address"):
        store.write(
            EncodedModbusValue(slave_id=1, register_type="holding", address=11, registers=(1,))
        )


def test_write_multi_word_crossing_declaration_boundary_is_key_error() -> None:
    store = ModbusSinkStore([_point(address=10, datatype="uint16")])
    with pytest.raises(KeyError, match="Undeclared Modbus address"):
        store.write(
            EncodedModbusValue(slave_id=1, register_type="holding", address=10, registers=(1, 2))
        )


def test_read_unknown_slave_is_key_error() -> None:
    store = ModbusSinkStore([_point()])
    with pytest.raises(KeyError, match="Unknown Modbus slave_id"):
        store.read_registers(3, "holding", 0)


def test_invalid_space_names_are_value_errors() -> None:
    store = ModbusSinkStore([_point()])
    with pytest.raises(ValueError, match="Not a bit register_type"):
        store.read_bits(1, "holding", 0)
    with pytest.raises(ValueError, match="Not a word register_type"):
        store.read_registers(1, "coil", 0)
