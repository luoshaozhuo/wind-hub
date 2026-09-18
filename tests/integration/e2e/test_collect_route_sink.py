"""端到端测试 —— 采集 → 路由 → sink 输出整条链路。"""

from __future__ import annotations

import asyncio

import pytest

from tests.fixtures.sinks.null_sink import NullSink
from wind_hub.assembly import AssembledRuntime
from wind_hub.domain.model.point import PointValue


def _null_sink(runtime: AssembledRuntime) -> NullSink:
    sink = runtime.sinks["null_sink"]
    assert isinstance(sink, NullSink)
    return sink


async def _wait_for_point(
    sink: NullSink,
    device_id: str,
    point_id: str,
    timeout: float = 5.0,
) -> PointValue:
    """轮询等待 null sink 收到指定点，超时则失败。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for pv in sink.received:
            if pv.device_id == device_id and pv.point_id == point_id:
                return pv
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"null sink did not receive {device_id}/{point_id} within {timeout}s "
        f"(received {[(p.device_id, p.point_id) for p in sink.received]})"
    )


async def test_collect_route_sink_roundtrip(runtime: AssembledRuntime) -> None:
    """采集到的 modbus 遥测点经路由落入 null sink，值正确。"""
    sink = _null_sink(runtime)

    pv = await _wait_for_point(sink, "modbus-1", "rotor.speed")
    assert pv.value == pytest.approx(1200.5)
    assert pv.source == "modbus"

    gen = await _wait_for_point(sink, "modbus-1", "gen.power")
    assert gen.value == pytest.approx(800.0)


async def test_collect_routes_all_modbus_points_to_null(runtime: AssembledRuntime) -> None:
    """modbus-1 的全部点位（含可写点）都被默认规则路由到 null sink。"""
    sink = _null_sink(runtime)

    await _wait_for_point(sink, "modbus-1", "temp.int")
    await _wait_for_point(sink, "modbus-1", "setpoint.power")

    received_ids = {(p.device_id, p.point_id) for p in sink.received}
    for point_id in ("rotor.speed", "gen.power", "temp.int", "setpoint.power"):
        assert ("modbus-1", point_id) in received_ids
