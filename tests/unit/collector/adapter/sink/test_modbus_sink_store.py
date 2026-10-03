"""Modbus Sink 内存数据区单元测试。"""

from wind_hub_collector.adapter.outbound.sink.modbus_codec import EncodedModbusValue
from wind_hub_collector.adapter.outbound.sink.modbus_store import ModbusSinkStore
from wind_hub_core.config.sinks import ModbusSinkAddress, ResolvedSinkPoint, SinkSource


def _point(
    point_id: str,
    *,
    datatype: str,
    register_type: str,
    address: int,
    unit_id: int = 1,
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id=point_id),
        ref=f"wt01.{point_id}",
        source_data_type=datatype,
        source_unit="none",
        datatype=datatype,
        unit="none",
        address=ModbusSinkAddress(
            unit_id=unit_id,
            register_type=register_type,  # type: ignore[arg-type]
            address=address,
        ),
    )


def test_store_preallocates_declared_ranges_with_zero() -> None:
    store = ModbusSinkStore(
        [
            _point("p1", datatype="float32", register_type="holding", address=100),
            _point("p2", datatype="bool", register_type="coil", address=10),
        ]
    )
    assert store.unit_ids == (1,)
    assert store.read_registers(1, "holding", 100, 2) == [0, 0]
    assert store.read_bits(1, "coil", 10) == [False]


def test_store_separates_units_and_register_types() -> None:
    store = ModbusSinkStore(
        [
            _point("p1", datatype="uint16", register_type="holding", address=5),
            _point("p2", datatype="uint16", register_type="input", address=5, unit_id=2),
        ]
    )
    assert store.unit_ids == (1, 2)
    assert store.read_registers(1, "holding", 5) == [0]
    assert store.read_registers(2, "input", 5) == [0]


def test_store_writes_encoded_registers() -> None:
    store = ModbusSinkStore(
        [_point("p1", datatype="float32", register_type="holding", address=100)]
    )
    store.write(
        EncodedModbusValue(
            unit_id=1,
            register_type="holding",
            address=100,
            registers=(0x3F80, 0x0000),
        )
    )
    assert store.read_registers(1, "holding", 100, 2) == [0x3F80, 0x0000]


def test_store_writes_encoded_bits() -> None:
    store = ModbusSinkStore(
        [_point("p1", datatype="bool", register_type="discrete", address=20)]
    )
    store.write(
        EncodedModbusValue(
            unit_id=1,
            register_type="discrete",
            address=20,
            bits=(True,),
        )
    )
    assert store.read_bits(1, "discrete", 20) == [True]


def test_store_rejects_undeclared_unit() -> None:
    store = ModbusSinkStore(
        [_point("p1", datatype="uint16", register_type="holding", address=5)]
    )
    try:
        store.write(
            EncodedModbusValue(
                unit_id=2,
                register_type="holding",
                address=5,
                registers=(1,),
            )
        )
    except KeyError as exc:
        assert "Unknown Modbus unit_id 2" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_store_rejects_write_outside_declared_span() -> None:
    store = ModbusSinkStore(
        [_point("p1", datatype="uint16", register_type="holding", address=5)]
    )
    try:
        store.write(
            EncodedModbusValue(
                unit_id=1,
                register_type="holding",
                address=5,
                registers=(1, 2),
            )
        )
    except KeyError as exc:
        assert "holding/6" in str(exc)
    else:
        raise AssertionError("expected KeyError")
