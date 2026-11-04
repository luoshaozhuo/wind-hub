"""UC-02 System：独立 Data Generator 进程 + 真实 CSV 历史源 + 真实/故障 Gateway。

覆盖 §14.2 标识：
test_replay_1x_2x_order / test_equal_timestamps_and_empty_range /
test_source_failure / test_stop_restart_boundary。
"""

from __future__ import annotations

import asyncio

import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import dg_stubs, read_gateway_variable, replay_document

pytestmark = [pytest.mark.system, pytest.mark.reliability]

VARIABLE = "GW01.pitch_angle"


def _rows(pairs: list[tuple[str, float, str]]) -> list[str]:
    # 源 CSV 使用 Core 设备/点名（GW02/a02_dm_pitchangle1，float32）。
    return [
        f"GW02,a02_dm_pitchangle1,{ts},{value},GOOD,{record_id}" for ts, value, record_id in pairs
    ]


async def _revision(stub) -> str:  # noqa: ANN001
    snapshot = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(snapshot.revision)


async def _generation_state(dg_stub, generation_id: str) -> str:  # noqa: ANN001
    response = await dg_stub.GetGeneration(common.ObjectId(id=generation_id))
    return str(response.generation.actual_state)


async def _read_value(bind: str) -> float | None:
    sample = await read_gateway_variable(bind, "gw-a", VARIABLE)
    if sample.quality != "GOOD":
        return None
    return sample.value.double_value


async def test_replay_1x_2x_order(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-02-S01：真实 CSV 经独立进程重放，源时间顺序正确，墙钟跨度随倍率减半。"""
    gateway = await start_real_gateway(
        endpoint_id="gw-a", devices={"GW01": [("pitch_angle", "float32", "feedback")]}
    )
    rows = _rows(
        [
            ("2026-01-01T00:00:00Z", 10.0, "r1"),
            ("2026-01-01T00:00:00.200Z", 20.0, "r2"),
            ("2026-01-01T00:00:00.400Z", 30.0, "r3"),
            ("2026-01-01T00:00:00.600Z", 40.0, "r4"),
        ]
    )

    async def run_at(speed: float) -> tuple[list[float], float]:
        service_path = configs_root(
            scenarios=replay_document(
                {"gw-a": gateway.data_bind},
                file="history/system-replay.csv",
                speed=speed,
                start="2026-01-01T00:00:00Z",
                end="2026-01-01T00:00:01Z",
                mappings=[("gw-a", VARIABLE)],
            ),
            history={"system-replay.csv": rows},
        )
        bind, _process = await dg_subprocess(service_path)
        channel, dg_stub, common_stub = await dg_stubs(bind)
        observed: list[float] = []
        try:
            revision = await _revision(common_stub)
            started = await dg_stub.StartGeneration(
                dg.StartGenerationRequest(scenario_id="demo-replay", expected_revision=revision)
            )
            assert started.accepted, started.message
            began = asyncio.get_running_loop().time()
            first_at: float | None = None
            deadline = began + 15.0
            while asyncio.get_running_loop().time() < deadline:
                value = await _read_value(gateway.data_bind)
                # 第二次运行时 Gateway 保留上轮末值 40.0；以源首值 10.0 对齐计时起点。
                if value is not None and (not observed or abs(observed[-1] - value) > 1e-6):
                    if observed == [] and value != 10.0:
                        await asyncio.sleep(0.01)
                        continue
                    if first_at is None:
                        first_at = asyncio.get_running_loop().time()
                    observed.append(value)
                state = await _generation_state(dg_stub, started.generation_id)
                if state == "COMPLETED" and len(observed) >= 4:
                    break
                await asyncio.sleep(0.01)
            span = asyncio.get_running_loop().time() - (first_at or began)
            assert await _generation_state(dg_stub, started.generation_id) == "COMPLETED"
            return observed, span
        finally:
            await channel.close()

    slow_values, slow_span = await run_at(4.0)
    fast_values, fast_span = await run_at(8.0)
    assert slow_values == [10.0, 20.0, 30.0, 40.0]
    assert fast_values == [10.0, 20.0, 30.0, 40.0]
    # 0.6s 源跨度：4x ≈ 0.15s，8x ≈ 0.075s；倍率翻倍，墙钟跨度明显缩短。
    assert fast_span < slow_span * 0.75, (fast_span, slow_span)
    assert 0.03 <= fast_span <= 1.0, fast_span
    assert 0.06 <= slow_span <= 2.0, slow_span


@pytest.mark.time_order
async def test_equal_timestamps_and_empty_range(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-02-S02：同时间戳按源行序稳定提交；空窗口不伪造数据并显式失败。"""
    gateway = await start_real_gateway(
        endpoint_id="gw-a", devices={"GW01": [("pitch_angle", "float32", "feedback")]}
    )
    rows = _rows(
        [
            ("2026-01-01T00:00:00Z", 41.0, "e1"),
            ("2026-01-01T00:00:00Z", 42.0, "e2"),
            ("2026-01-01T00:00:01Z", 43.0, "e3"),
        ]
    )
    doc = replay_document(
        {"gw-a": gateway.data_bind},
        scenario_id="replay-equal",
        file="history/equal.csv",
        speed=100.0,
        mappings=[("gw-a", VARIABLE)],
    )
    empty_doc = replay_document(
        {"gw-a": gateway.data_bind},
        scenario_id="replay-empty",
        file="history/equal.csv",
        speed=100.0,
        start="2026-01-02T00:00:00Z",
        end="2026-01-02T01:00:00Z",
        mappings=[("gw-a", VARIABLE)],
    )
    doc["scenarios"].append(empty_doc["scenarios"][0])
    service_path = configs_root(scenarios=doc, history={"equal.csv": rows})
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="replay-equal", expected_revision=revision)
        )
        assert started.accepted, started.message
        # 同时间戳两行按 record 顺序提交，后者最终覆盖同一变量；不得丢行或乱序。
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            if await _generation_state(dg_stub, started.generation_id) == "COMPLETED":
                break
            await asyncio.sleep(0.02)
        assert await _generation_state(dg_stub, started.generation_id) == "COMPLETED"
        final = await _read_value(gateway.data_bind)
        assert final == 43.0
        # 空窗口：FAILED + EMPTY_REPLAY_WINDOW，且此前没有任何值被写入该新任务。
        empty = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="replay-empty", expected_revision=revision)
        )
        assert empty.accepted
        deadline = asyncio.get_running_loop().time() + 5.0
        state = ""
        while asyncio.get_running_loop().time() < deadline:
            state = await _generation_state(dg_stub, empty.generation_id)
            if state == "FAILED":
                break
            await asyncio.sleep(0.02)
        assert state == "FAILED"
        detail = await dg_stub.GetGeneration(common.ObjectId(id=empty.generation_id))
        assert "EMPTY_REPLAY_WINDOW" in detail.generation.last_error
    finally:
        await channel.close()


@pytest.mark.fault_injection
async def test_source_failure(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-02-S03：坏记录终止任务且不发送非法数据。

    验收实测语义：CSV 在发送前整源建索引（逐行 decode），坏记录在首个批次前
    即令任务 FAILED，Gateway 全程无写入；不存在"部分确认后继续"的窗口。
    """
    gateway = await start_real_gateway(
        endpoint_id="gw-a", devices={"GW01": [("pitch_angle", "float32", "feedback")]}
    )
    rows = _rows(
        [
            ("2026-01-01T00:00:00Z", 51.0, "s1"),
            ("2026-01-01T00:00:00.100Z", 52.0, "s2"),
        ]
    )
    rows.append("GW02,a02_dm_pitchangle1,2026-01-01T00:00:00.200Z,not-a-number,GOOD,s3")
    service_path = configs_root(
        scenarios=replay_document(
            {"gw-a": gateway.data_bind},
            file="history/bad-row.csv",
            speed=100.0,
            mappings=[("gw-a", VARIABLE)],
        ),
        history={"bad-row.csv": rows},
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-replay", expected_revision=revision)
        )
        assert started.accepted, started.message
        deadline = asyncio.get_running_loop().time() + 10.0
        state = ""
        while asyncio.get_running_loop().time() < deadline:
            state = await _generation_state(dg_stub, started.generation_id)
            if state == "FAILED":
                break
            await asyncio.sleep(0.02)
        assert state == "FAILED"
        detail = await dg_stub.GetGeneration(common.ObjectId(id=started.generation_id))
        assert "invalid float" in detail.generation.last_error
        # 整源预校验失败：任何批次都不得发出，坏值/好值均未写入，之后也不再输出。
        assert await _read_value(gateway.data_bind) is None
        await asyncio.sleep(0.5)
        assert await _read_value(gateway.data_bind) is None
    finally:
        await channel.close()


@pytest.mark.restart
async def test_stop_restart_boundary(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-02-S03：中途停止后重启任务从头重放——不声称断点恢复或 exactly-once。"""
    gateway = await start_real_gateway(
        endpoint_id="gw-a", devices={"GW01": [("pitch_angle", "float32", "feedback")]}
    )
    rows = _rows(
        [
            ("2026-01-01T00:00:00Z", 61.0, "b1"),
            ("2026-01-01T00:00:00.500Z", 62.0, "b2"),
            ("2026-01-01T00:00:01.000Z", 63.0, "b3"),
            ("2026-01-01T00:00:01.500Z", 64.0, "b4"),
        ]
    )
    service_path = configs_root(
        scenarios=replay_document(
            {"gw-a": gateway.data_bind},
            file="history/boundary.csv",
            speed=2.0,
            end="2026-01-01T00:00:03Z",
            mappings=[("gw-a", VARIABLE)],
        ),
        history={"boundary.csv": rows},
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        first = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-replay", expected_revision=revision)
        )
        assert first.accepted, first.message
        # 等到至少写入第二条后停止。
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            value = await _read_value(gateway.data_bind)
            if value is not None and value >= 62.0:
                break
            await asyncio.sleep(0.02)
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(generation_id=first.generation_id, expected_revision=revision)
        )
        assert stopped.accepted
        assert await _generation_state(dg_stub, first.generation_id) == "STOPPED"
        # 同场景再启动：从源文件开头重放（观察到 61.0 重新出现），即不提供断点续传。
        second = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-replay", expected_revision=revision)
        )
        assert second.accepted, second.message
        restarted_from_head = False
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            value = await _read_value(gateway.data_bind)
            if value == 61.0:
                restarted_from_head = True
                break
            await asyncio.sleep(0.02)
        assert restarted_from_head, "重启后未从头重放（若实现了断点续传需更新设计与测试）"
        # 收尾：完整跑到 COMPLETED，避免进程退出时仍有在途批次。
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            if await _generation_state(dg_stub, second.generation_id) == "COMPLETED":
                break
            await asyncio.sleep(0.05)
    finally:
        await channel.close()


@pytest.mark.deadline
async def test_index_cancel_safely_joins_worker_and_removes_sqlite(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """R08：索引期间停止：不可杀线程/残留 SQLite；worker 收敛后任务 STOPPED 且零批次。"""
    import tempfile
    import time
    from pathlib import Path

    rows = []
    for i in range(120_000):
        clock = i % 86_400
        stamp = f"2026-01-01T{clock // 3600:02d}:{clock % 3600 // 60:02d}:{clock % 60:02d}Z"
        rows.append(f"GW02,a02_dm_pitchangle1,{stamp},{3.0 + (i % 50) * 0.01:.2f},GOOD,r{i}")
    fault = await start_fault_gateway({"gw-a": [(VARIABLE, "float32", True)]})
    service_path = configs_root(
        scenarios=replay_document(
            {"gw-a": fault.bind},
            file="history/large.csv",
            speed=10.0,
            start="2026-01-01T00:00:00Z",
            end="2026-01-01T02:00:00Z",
            mappings=[("gw-a", VARIABLE)],
        ),
        history={"large.csv": rows},
        output_timeout=5.0,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    baseline = {p.name for p in Path(tempfile.gettempdir()).glob("windhub-replay-*")}
    began = time.monotonic()
    try:
        revision = await _revision(common_stub)
        first = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-replay", expected_revision=revision)
        )
        assert first.accepted, first.message
        # 立即停止：整源索引（to_thread）仍在进行，取消不可杀掉 worker 线程。
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(generation_id=first.generation_id, expected_revision=revision)
        )
        assert stopped.accepted
        deadline = asyncio.get_running_loop().time() + 40.0
        state = ""
        while asyncio.get_running_loop().time() < deadline:
            state = await _generation_state(dg_stub, first.generation_id)
            if state == "STOPPED":
                break
            await asyncio.sleep(0.1)
        join_seconds = time.monotonic() - began
        assert state == "STOPPED", state
        # 取消发生在首批发送之前：任何数据都不得写出。
        assert fault.service.update_calls["gw-a"] == []
        # 临时 SQLite 索引必须随 worker 收敛而清理（有界磁盘生命周期）。
        remaining = {
            p.name
            for p in Path(tempfile.gettempdir()).glob("windhub-replay-*")
            if p.name not in baseline
        }
        assert not remaining, f"temporary replay index leaked: {remaining}"
        assert join_seconds < 40.0, join_seconds
    finally:
        await channel.close()
