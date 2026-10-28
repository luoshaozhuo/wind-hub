"""ModbusSinkDataPath 数据更新路径测试（纯内存）。

- GOOD 点值经 export/encode 落库并返回更新计数；
- BAD/UNCERTAIN/None 不覆盖最近一次 GOOD 值；
- 未映射点忽略；update_selected 只写给定定义子集。
"""

from __future__ import annotations

from core.application import Quality
from core.application.sink_contract import (
    ModbusSinkAddress,
    ResolvedSinkPoint,
    SinkSource,
)
from core.domain.point_value import PointValue
from core.infrastructure.sink.modbus.pipeline import ModbusSinkDataPath


def _point(ref: str = "scada.wind_speed", datatype: str = "uint16", address: int = 0):
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id="wind_speed"),
        ref=ref,
        source_data_type="uint16",
        source_unit="none",
        datatype=datatype,
        unit="none",
        address=ModbusSinkAddress(slave_id=1, register_type="holding", address=address),
    )


def _pv(value, *, quality=Quality.GOOD) -> PointValue:
    return PointValue(device_id="wt01", point_id="wind_speed", value=value, quality=quality)


def test_update_returns_count_and_writes_store() -> None:
    path = ModbusSinkDataPath([_point()])

    updated = path.update([_pv(0x1234)])

    assert updated == 1
    assert path.store.read_registers(1, "holding", 0) == [0x1234]


def test_bad_and_none_values_keep_last_good() -> None:
    path = ModbusSinkDataPath([_point()])
    assert path.update([_pv(7)]) == 1

    assert path.update([_pv(0, quality=Quality.BAD)]) == 0
    assert path.update([_pv(None, quality=Quality.UNCERTAIN)]) == 0

    # 最近一次 GOOD 值不被质量差的数据覆盖。
    assert path.store.read_registers(1, "holding", 0) == [7]


def test_unmapped_points_are_ignored() -> None:
    path = ModbusSinkDataPath([_point()])
    updated = path.update(
        [PointValue(device_id="other", point_id="x", value=1, quality=Quality.GOOD)]
    )
    assert updated == 0


def test_update_selected_only_writes_given_definitions() -> None:
    selected = _point(address=100)
    path = ModbusSinkDataPath([_point(), selected])

    updated = path.update_selected([_pv(9)], [selected])

    assert updated == 1
    assert path.store.read_registers(1, "holding", 100) == [9]
    # 主映射地址未被写。
    assert path.store.read_registers(1, "holding", 0) == [0]
