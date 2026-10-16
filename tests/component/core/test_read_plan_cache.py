"""高频轮询读取计划的复用与失效测试。"""

from __future__ import annotations

import struct

import pytest

from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads.driver import ADSDriver, _AdsSession
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from tests.support.units import NONE_UNIT


class _StubClient:
    """仅提供连接状态的最小 pymodbus client 替身（读写已被 monkeypatch）。"""

    connected = True


def _point(
    point_id: str,
    ext: dict[str, str | int | float | bool | None],
    *,
    access: PointAccess = PointAccess.READ_WRITE,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext=ext,
    )


def test_modbus_reuses_plan_and_caps_dynamic_cache():
    points = {
        f"p{i}": _point(f"p{i}", {"register_type": "holding", "address": i, "data_type": "uint16"})
        for i in range(40)
    }
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable(PointTableId("mb"), Protocol("modbus"), points),
        {},
    )
    first = driver._read_plan(("p0", "p1"))
    assert driver._read_plan(["p0", "p1"]) is first
    assert driver._read_plan(("p1", "p0")) is not first
    for i in range(40):
        driver._read_plan((f"p{i}",))
    assert len(driver._read_plan_cache) <= 32


class _SumReadConn:
    """记录 Sum Read 请求地址定义并返回预置原始帧的连接替身。"""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.seen: list[tuple[tuple[int, int, int], ...]] = []

    def read_write(self, _group, _offset, _datatype, data, _buffer, *, check_length):
        del _group, _offset, _datatype, _buffer, check_length
        self.seen.append(tuple((item.iGroup, item.iOffset, item.size) for item in data))
        return self.raw


@pytest.mark.asyncio
async def test_ads_sum_plan_reuses_groups_and_invalidates_on_session_reset(monkeypatch):
    points = {
        "a": _point("a", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 0}),
        "b": _point("b", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 4}),
    }
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable(PointTableId("ads"), Protocol("ads"), points),
        {},
    )
    conn = _SumReadConn(struct.pack("<IIii", 0, 0, 11, 22))
    driver._session = _AdsSession(conn)
    driver._connected = True

    builds = 0
    original_build = driver._build_sum_requests

    def counting_build(key):
        nonlocal builds
        builds += 1
        return original_build(key)

    monkeypatch.setattr(driver, "_build_sum_requests", counting_build)

    first = await driver.read_many(["a", "b"])
    plan = next(iter(driver._read_cache.values()))
    second = await driver.read_many(["a", "b"])
    assert builds == 1  # 第二次命中预编译计划，不重新分组/编译
    assert [(x.value, x.quality) for x in first] == [(x.value, x.quality) for x in second]
    assert [x.value for x in second] == [11, 22]
    assert next(iter(driver._read_cache.values()))[0] is plan[0]
    assert conn.seen == [((0x4020, 0, 4), (0x4020, 4, 4))] * 2

    driver._invalidate_symbol_addresses()
    assert driver._read_cache == {}
    await driver.read_many(["a", "b"])
    assert builds == 2  # 失效后重新编译


@pytest.mark.asyncio
async def test_modbus_internal_values_and_wrapped_samples(monkeypatch):
    point = _point("p0", {"register_type": "holding", "address": 10, "data_type": "uint16"})
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable(PointTableId("mb"), Protocol("modbus"), {"p0": point}),
        {},
    )
    driver._client = _StubClient()

    async def read_group(group):
        # 值字典以 ModbusPoint（完整地址定义）为键
        return {point: 17 for point in group.points}

    monkeypatch.setattr(driver, "_read_group", read_group)
    mapped_points, raw = await driver._read_values(["p0"])
    assert mapped_points[0].point_id == "p0"
    assert raw[0][0] == 17
    from core.application import Quality

    assert raw[0][1] is Quality.GOOD
    wrapped = await driver.read_many(["p0"])
    assert wrapped[0].point_id == "p0"
    assert wrapped[0].value == 17


@pytest.mark.asyncio
async def test_modbus_read_many_preserves_bad_quality(monkeypatch):
    point = _point("p0", {"register_type": "holding", "address": 10, "data_type": "uint16"})
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable(PointTableId("mb"), Protocol("modbus"), {"p0": point}),
        {},
    )
    driver._client = _StubClient()

    async def read_group(_group):
        return {}

    monkeypatch.setattr(driver, "_read_group", read_group)
    from core.application import Quality

    samples = await driver.read_many(["p0"])
    assert [(sample.value, sample.quality) for sample in samples] == [(None, Quality.BAD)]


@pytest.mark.asyncio
async def test_ads_internal_values_and_wrapped_samples_preserve_bad(monkeypatch):
    from core.application import Quality

    points = {
        "good": _point("good", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 0}),
        "bad": _point("bad", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 4}),
    }
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable(PointTableId("ads"), Protocol("ads"), points),
        {},
    )
    # 首个错误码 0 + DINT 23；第二个错误码 1808（设备点级错误）→ BAD。
    driver._session = _AdsSession(_SumReadConn(struct.pack("<IIii", 0, 1808, 23, 0)))
    driver._connected = True

    assert await driver._read_values(["good", "bad"]) == (
        (23, Quality.GOOD),
        (None, Quality.BAD),
    )
    wrapped = await driver.read_many(["good", "bad"])
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
        PointTable(PointTableId("mb"), Protocol("modbus"), points),
        {},
    )

    class Response:
        registers = (11, 22)

        def isError(self):  # noqa: N802 — pymodbus compatibility
            return False

    class Client:
        connected = True

        async def read_holding_registers(self, address, *, count, device_id):
            assert (address, count) == (10, 2)
            return Response()

    driver._client = Client()
    from core.application import Quality

    samples = await driver.read_many(("a", "b"))
    assert [(sample.value, sample.quality) for sample in samples] == [
        (11, Quality.GOOD),
        (22, Quality.GOOD),
    ]
    assert Response.registers == (11, 22)


def test_modbus_read_plan_precomputes_register_ranges():
    """同一读取计划在热路径上不再扫描组内寄存器计算起止地址。"""
    points = {
        "x": _point("x", {"register_type": "holding", "address": 12, "data_type": "uint16"}),
        "y": _point("y", {"register_type": "holding", "address": 13, "data_type": "uint16"}),
        "z": _point(
            "z",
            {"register_type": "input", "address": 8, "data_type": "uint16"},
            access=PointAccess.READ,
        ),
    }
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable(PointTableId("mb"), Protocol("modbus"), points),
        {},
    )
    first = driver._read_plan(("x", "y", "z"))
    assert sorted((group.register_type, group.start, group.count) for group in first) == [
        ("holding", 12, 2),
        ("input", 8, 1),
    ]
    assert first is driver._read_plan(("x", "y", "z"))


@pytest.mark.asyncio
async def test_ads_sum_read_survives_point_table_reload_mid_flight(monkeypatch):
    """在途批量读期间发生热重载：本轮读用局部快照完成，不误判传输故障拆连接。"""
    points = {
        "a": _point("a", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 0}),
    }
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable(PointTableId("ads"), Protocol("ads"), points),
        {},
    )
    class _ReloadConn:
        def read_write(self, _group, _offset, _datatype, _data, _buffer, *, check_length):
            del self, _group, _offset, _datatype, _data, _buffer, check_length
            # 模拟在途读期间发生热重载：读计划缓存被整体清空。
            driver.update_point_table(PointTable(PointTableId("ads"), Protocol("ads"), points))
            return struct.pack("<Ii", 0, 11)

    driver._session = _AdsSession(_ReloadConn())
    driver._connected = True

    result = await driver.read_many(["a"])
    from core.application import Quality

    assert [(x.value, x.quality) for x in result] == [(11, Quality.GOOD)]


@pytest.mark.asyncio
async def test_ads_mixed_plan_survives_point_table_reload_mid_flight():
    """固定尺寸 DINT + 变长 STRING 同一 Sum Read 批次：返回前热重载，
    本轮仍用调用时解析的局部点快照完成解码，不把热重载误判为传输故障。"""
    points = {
        "a": _point("a", {"data_type": "DINT", "index_group": 0x4020, "index_offset": 0}),
        "s": _point(
            "s",
            {
                "data_type": "STRING",
                "size": 4,
                "index_group": 0x4020,
                "index_offset": 16,
            },
        ),
    }
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 801),
        PointTable(PointTableId("ads"), Protocol("ads"), points),
        {},
    )

    class Conn:
        def read_write(self, _group, _offset, _datatype, _data, _buffer, *, check_length):
            del self, _group, _offset, _datatype, _data, _buffer, check_length
            # sum read 返回前发生热重载：预编译计划缓存被清空。
            driver.update_point_table(PointTable(PointTableId("ads"), Protocol("ads"), points))
            return struct.pack("<IIi", 0, 0, 11) + b"ok\x00\x00"

    driver._session = _AdsSession(Conn())
    driver._connected = True

    result = await driver.read_many(["a", "s"])
    from core.application import Quality

    assert [(x.value, x.quality) for x in result] == [(11, Quality.GOOD), ("ok", Quality.GOOD)]


def test_modbus_plan_cache_invalidates_on_point_table_update():
    points = {
        "p0": _point("p0", {"register_type": "holding", "address": 0, "data_type": "uint16"}),
    }
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable(PointTableId("mb"), Protocol("modbus"), points),
        {},
    )
    assert driver._read_plan(("p0",))
    assert driver._read_plan_cache

    driver.update_point_table(PointTable(PointTableId("mb"), Protocol("modbus"), points))
    assert driver._read_plan_cache == {}
