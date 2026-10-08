"""Modbus Sink 数据路径单元测试。"""

from wind_hub_collector.adapter.outbound.sink.modbus_pipeline import ModbusSinkDataPath
from wind_hub_core.config import ModbusSinkAddress, ResolvedSinkPoint, SinkSource
from wind_hub_core.model.point import PointValue, Quality


def _point(
    point_id: str,
    *,
    datatype: str,
    register_type: str,
    address: int,
    scale: float = 1.0,
    offset: float = 0.0,
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id=point_id),
        ref=f"wt01.{point_id}",
        source_data_type=datatype,
        source_unit="none",
        datatype=datatype,
        unit="none",
        scale=scale,
        offset=offset,
        address=ModbusSinkAddress(
            unit_id=1,
            register_type=register_type,  # type: ignore[arg-type]
            address=address,
        ),
    )


def test_update_exports_encodes_and_writes_registers() -> None:
    path = ModbusSinkDataPath(
        [_point("p1", datatype="float32", register_type="holding", address=100)]
    )
    count = path.update([PointValue(device_id="wt01", point_id="p1", value=1.0)])
    assert count == 1
    assert path.store.read_registers(1, "holding", 100, 2) == [0x3F80, 0x0000]


def test_update_applies_sink_scale_offset_before_encoding() -> None:
    path = ModbusSinkDataPath(
        [
            _point(
                "p1",
                datatype="float32",
                register_type="holding",
                address=100,
                scale=2.0,
                offset=1.0,
            )
        ]
    )
    path.update([PointValue(device_id="wt01", point_id="p1", value=1.0)])
    assert path.store.read_registers(1, "holding", 100, 2) == [0x4040, 0x0000]


def test_update_ignores_unmapped_input() -> None:
    path = ModbusSinkDataPath(
        [_point("p1", datatype="uint16", register_type="holding", address=10)]
    )
    count = path.update([PointValue(device_id="wt01", point_id="other", value=7)])
    assert count == 0
    assert path.store.read_registers(1, "holding", 10) == [0]


def test_bad_value_does_not_overwrite_last_good_registers() -> None:
    path = ModbusSinkDataPath(
        [_point("p1", datatype="uint16", register_type="holding", address=10)]
    )
    path.update([PointValue(device_id="wt01", point_id="p1", value=7)])
    count = path.update(
        [
            PointValue(
                device_id="wt01",
                point_id="p1",
                value=99,
                quality=Quality.BAD,
            )
        ]
    )
    assert count == 0
    assert path.store.read_registers(1, "holding", 10) == [7]


def test_none_value_does_not_overwrite_last_good_registers() -> None:
    path = ModbusSinkDataPath(
        [_point("p1", datatype="uint16", register_type="holding", address=10)]
    )
    path.update([PointValue(device_id="wt01", point_id="p1", value=7)])
    count = path.update([PointValue(device_id="wt01", point_id="p1", value=None)])
    assert count == 0
    assert path.store.read_registers(1, "holding", 10) == [7]


def test_update_writes_bit_space() -> None:
    path = ModbusSinkDataPath([_point("run", datatype="bool", register_type="coil", address=5)])
    count = path.update([PointValue(device_id="wt01", point_id="run", value=True)])
    assert count == 1
    assert path.store.read_bits(1, "coil", 5) == [True]
