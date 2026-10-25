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


def _plan(driver: ModbusDriver, points: tuple[str, ...] | list[str]):
    """以已解析测点构造读取计划，和 Driver 生产调用保持一致。"""
    return driver._read_plan(points, tuple(driver._resolve_point(point) for point in points))


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
    first = _plan(driver, ("p0", "p1"))
    assert _plan(driver, ["p0", "p1"]) is first
    assert _plan(driver, ("p1", "p0")) is not first
    for i in range(40):
        _plan(driver, (f"p{i}",))
    assert len(driver._read_plan_cache) <= 32


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
    first = _plan(driver, ("x", "y", "z"))
    assert sorted((group.register_type, group.start, group.count) for group in first) == [
        ("holding", 12, 2),
        ("input", 8, 1),
    ]
    assert first is _plan(driver, ("x", "y", "z"))


def test_modbus_plan_cache_invalidates_on_point_table_update():
    points = {
        "p0": _point("p0", {"register_type": "holding", "address": 0, "data_type": "uint16"}),
    }
    driver = ModbusDriver(
        ConnectionEndpoint("127.0.0.1", 502),
        PointTable(PointTableId("mb"), Protocol("modbus"), points),
        {},
    )
    assert _plan(driver, ("p0",))
    assert driver._read_plan_cache

    driver.update_point_table(PointTable(PointTableId("mb"), Protocol("modbus"), points))
    assert driver._read_plan_cache == {}
