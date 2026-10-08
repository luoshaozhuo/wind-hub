"""高频轮询读取计划的复用与失效测试。"""

from __future__ import annotations

import struct

import pytest

from core.domain import ConnectionEndpoint, Point, PointAccess, PointTable, Protocol, UNIT_CATALOG, UnitCode
from core.infrastructure.protocol.ads.driver import ADSDriver
from core.infrastructure.protocol.modbus.driver import ModbusDriver


def _point(point_id: str, ext: dict[str, object]) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext=ext,
    )


def test_modbus_reuses_plan_and_caps_dynamic_cache():
    points = {
        f"p{i}": _point(f"p{i}", {"register_type": "holding", "address": i, "data_type": "uint16"})
        for i in range(40)
    }
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable("mb", Protocol("modbus"), points),
        {},
    )
    first = driver._read_plan(("p0", "p1"))
    assert driver._read_plan(["p0", "p1"]) is first
    assert driver._read_plan(("p1", "p0")) is not first
    for i in range(40):
        driver._read_plan((f"p{i}",))
    assert len(driver._read_plan_cache) <= 32


@pytest.mark.asyncio
async def test_ads_sum_plan_reuses_groups_and_invalidates_on_session_reset(monkeypatch):
    points = {
        "a": _point("a", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 0}),
        "b": _point("b", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 4}),
    }
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable("ads", Protocol("ads"), points),
        {},
    )
    driver._connection = object()
    driver._connected = True
    seen = []

    def _read(addresses):
        seen.append(tuple(addresses))
        return struct.pack("<IIii", 0, 0, 11, 22)

    monkeypatch.setattr(driver, "_sum_read_bytes", _read)
    # DINT 解码需要 pyads 类型；采用独立的确定性解码替身。
    import core.infrastructure.protocol.ads.driver as ads_module

    monkeypatch.setattr(ads_module, "_decode_value", lambda raw, _point: struct.unpack("<i", raw)[0])
    first = await driver.read(["a", "b"])
    plan = driver._read_plan_cache[("a", "b")]
    second = await driver.read(["a", "b"])
    assert [(x.value, x.quality) for x in first] == [(x.value, x.quality) for x in second]
    assert [x.value for x in second] == [11, 22]
    assert driver._read_plan_cache[("a", "b")] is plan
    assert seen == [((0x4020, 0, 4), (0x4020, 4, 4))] * 2
    driver._invalidate_symbol_addresses()
    assert driver._read_plan_cache == {}


@pytest.mark.asyncio
async def test_modbus_raw_read_does_not_create_sample_dto(monkeypatch):
    point = _point("p0", {"register_type": "holding", "address": 10, "data_type": "uint16"})
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable("mb", Protocol("modbus"), {"p0": point}),
        {},
    )
    driver._connected = True

    async def read_group(_group):
        return {"p0": 17}

    monkeypatch.setattr(driver, "_read_group", read_group)
    raw = await driver.read_raw(["p0"])
    assert raw[0][0] == 17
    from core.application import Quality

    assert raw[0][1] is Quality.GOOD
    wrapped = await driver.read(["p0"])
    assert wrapped[0].point_id == "p0"
    assert wrapped[0].value == 17


@pytest.mark.asyncio
async def test_modbus_raw_read_preserves_bad_quality(monkeypatch):
    point = _point("p0", {"register_type": "holding", "address": 10, "data_type": "uint16"})
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable("mb", Protocol("modbus"), {"p0": point}),
        {},
    )
    driver._connected = True

    async def read_group(_group):
        return {}

    monkeypatch.setattr(driver, "_read_group", read_group)
    from core.application import Quality

    assert await driver.read_raw(["p0"]) == ((None, Quality.BAD),)


@pytest.mark.asyncio
async def test_ads_raw_read_skips_sample_wrapping_and_preserves_bad(monkeypatch):
    from core.application import Quality

    points = {
        "good": _point("good", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 0}),
        "bad": _point("bad", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 4}),
    }
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable("ads", Protocol("ads"), points),
        {},
    )
    driver._connection = object()
    driver._connected = True

    monkeypatch.setattr(
        driver,
        "_sum_read_bytes",
        lambda _addresses: struct.pack("<IIii", 0, 1808, 23, 0),
    )
    import core.infrastructure.protocol.ads.driver as ads_module

    monkeypatch.setattr(
        ads_module,
        "_decode_value",
        lambda data, _mapped: struct.unpack("<i", data)[0],
    )
    assert await driver.read_raw(["good", "bad"]) == (
        (23, Quality.GOOD),
        (None, Quality.BAD),
    )
    wrapped = await driver.read(["good", "bad"])
    assert [sample.point_id for sample in wrapped] == ["good", "bad"]
    assert wrapped[0].value == 23
    assert wrapped[1].quality is Quality.BAD


@pytest.mark.asyncio
async def test_modbus_accepts_tuple_register_buffer_without_copying():
    """底层响应允许只读寄存器序列；多个相邻点解码不改变原始缓冲区。"""
    points = {
        "a": _point("a", {"register_type": "holding", "address": 10, "data_type": "uint16"}),
        "b": _point("b", {"register_type": "holding", "address": 11, "data_type": "uint16"}),
    }
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable("mb", Protocol("modbus"), points),
        {},
    )

    class Response:
        registers = (11, 22)

        def isError(self):
            return False

    class Client:
        async def read_holding_registers(self, address, *, count, device_id):
            assert (address, count) == (10, 2)
            return Response()

    driver._client = Client()
    driver._connected = True
    from core.application import Quality

    assert await driver.read_raw(("a", "b")) == (
        (11, Quality.GOOD),
        (22, Quality.GOOD),
    )
    assert Response.registers == (11, 22)
