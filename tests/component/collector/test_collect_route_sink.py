"""端到端测试 —— Task → 采集 → targets sink 输出整条链路。"""

from __future__ import annotations

import asyncio

import pytest

from tests.fixtures.sinks.null_sink import NullSink
from wind_hub_collector.assembly import CollectorApp
from wind_hub_core.model.point import PointValue

from .runtime_helpers import start_task_instance


def _null_sink(runtime: CollectorApp) -> NullSink:
    sink = runtime.runtime.sinks["null_sink"]
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


async def test_task_instances_expanded_and_started(runtime: CollectorApp) -> None:
    """tasks.yaml 的 Task 按设备展开为实例，启动后处于 RUNNING。"""
    instances = await runtime.tasks.list_instances()
    by_id = {i.instance_id: i for i in instances}
    assert "modbus-telemetry:modbus-1" in by_id
    assert "iec104-telemetry:iec104-1" in by_id
    assert by_id["modbus-telemetry:modbus-1"].targets == ["null_sink"]
    assert by_id["modbus-telemetry:modbus-1"].state.value == "running"


async def test_task_collect_targets_sink_roundtrip(runtime: CollectorApp) -> None:
    """modbus telemetry Task 采集到的点经 targets 落入 null sink，值正确。"""
    sink = _null_sink(runtime)

    pv = await _wait_for_point(sink, "modbus-1", "rotor.speed")
    assert pv.value == pytest.approx(1200.5)
    assert pv.source == "modbus"

    gen = await _wait_for_point(sink, "modbus-1", "gen.power")
    assert gen.value == pytest.approx(800.0)


async def test_task_collects_all_modbus_points_to_null(runtime: CollectorApp) -> None:
    """modbus-1 的 telemetry 组全部点位（含可写点）都经 Task targets 落入 null sink。"""
    sink = _null_sink(runtime)

    await _wait_for_point(sink, "modbus-1", "temp.int")
    await _wait_for_point(sink, "modbus-1", "setpoint.power")

    received_ids = {(p.device_id, p.point_id) for p in sink.received}
    for point_id in ("rotor.speed", "gen.power", "temp.int", "setpoint.power"):
        assert ("modbus-1", point_id) in received_ids


async def test_stopped_instance_stops_collection(runtime: CollectorApp) -> None:
    """stop 单个实例后不再有点落入 sink；重新 start 后恢复采集。"""
    sink = _null_sink(runtime)
    iid = "modbus-telemetry:modbus-1"

    # 先确认运行中有点落入，再停实例。
    await _wait_for_point(sink, "modbus-1", "rotor.speed")
    detail = await runtime.tasks.stop_instance(iid)
    assert detail.state.value == "stopped"

    # 只数 modbus-1 的点——iec104 实例仍在运行，不能比较 sink 总数。
    # 先等一拍让已入队的在途批次落完，再取基线。
    await asyncio.sleep(0.1)
    marker = sum(1 for p in sink.received if p.device_id == "modbus-1")
    await asyncio.sleep(0.6)  # interval 0.2s 的 3 倍——运行中必然已产出新点
    assert sum(1 for p in sink.received if p.device_id == "modbus-1") == marker

    # 显式 start（幂等路径）后恢复采集。
    detail = await start_task_instance(runtime, iid)
    assert detail.state.value == "running"
    await _wait_for_point(sink, "modbus-1", "rotor.speed")
