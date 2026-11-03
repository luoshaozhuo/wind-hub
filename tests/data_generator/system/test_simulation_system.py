"""UC-01/UC-03 System：独立 Data Generator 进程 + 真实（或正式契约故障注入）Gateway。

覆盖 §14.2 标识：
test_random_generation / test_static_physics /
test_invalid_model_and_runtime_failure / test_overrun_and_stop /
test_gateway_feedback / test_bad_gateway_mapping /
test_fanout_multiple_gateways_and_variables / test_conflicting_target_ownership /
test_duplicate_target_mapping_rejected。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import (
    SRC_PATH,
    dg_stubs,
    metric_map,
    read_gateway_variable,
    replace_snapshot_files,
    ship_scenarios_document,
    simulation_document,
)

pytestmark = pytest.mark.system

PITCH = "a02_dm_pitchangle1"
RATE = "a03_dm_pitchratedemand1"
ONE_FLOAT_DEVICE = {"GW01": [("pitch_angle", "float32", "feedback")]}
TWO_FLOAT_DEVICE = {
    "GW01": [
        ("pitch_angle", "float32", "feedback"),
        ("pitch_rate", "float32", "feedback"),
    ]
}
REPO_ROOT = Path(__file__).resolve().parents[3]


async def _runtime_revision(stub) -> str:  # noqa: ANN001
    snapshot = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(snapshot.revision)


async def _start(stub, revision: str, scenario: str = "demo-random"):  # noqa: ANN001
    response = await stub.StartGeneration(
        dg.StartGenerationRequest(scenario_id=scenario, expected_revision=revision)
    )
    assert response.accepted, response.message
    return response.generation_id


async def _wait_changing_value(
    bind: str,
    gateway_id: str,
    variable: str,
    *,
    samples: int = 3,
    timeout: float = 10.0,
) -> list[float]:
    seen: list[float] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        sample = await read_gateway_variable(bind, gateway_id, variable)
        if sample.quality == "GOOD":
            value = sample.value.double_value
            if not seen or abs(seen[-1] - value) > 1e-6:
                seen.append(value)
                if len(seen) >= samples:
                    return seen
        await asyncio.sleep(0.1)
    raise AssertionError(f"only observed {seen} changes for {gateway_id}:{variable}")


def _scenario_path(snapshot: Any) -> str:  # noqa: ANN401
    return str(
        next(
            item.relative_path
            for item in snapshot.files
            if item.relative_path.endswith("instances/data-generator.yaml")
        )
    )


@pytest.mark.reliability
@pytest.mark.end_to_end
async def test_random_generation(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-01-S01：独立进程持续随机生成并经正式 UpdateVariables 写入，停止后无额外输出。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT_DEVICE)
    doc = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.15,
        minimum=3.0,
        maximum=15.0,
        mappings=[("gw-a", "GW01.pitch_angle")],
    )
    service_path = configs_root(scenarios=doc)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        generation_id = await _start(dg_stub, revision)
        values = await _wait_changing_value(
            gateway.data_bind, "gw-a", "GW01.pitch_angle", samples=3
        )
        assert len({round(v, 6) for v in values}) >= 2
        assert all(3.0 - 1e-6 <= v <= 15.0 + 1e-6 for v in values)

        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(generation_id=generation_id, expected_revision=revision)
        )
        assert stopped.accepted
        await asyncio.sleep(0.45)
        after_stop_1 = await read_gateway_variable(gateway.data_bind, "gw-a", "GW01.pitch_angle")
        await asyncio.sleep(0.45)
        after_stop_2 = await read_gateway_variable(gateway.data_bind, "gw-a", "GW01.pitch_angle")
        assert after_stop_1.quality == after_stop_2.quality == "GOOD"
        assert after_stop_1.value.double_value == after_stop_2.value.double_value
    finally:
        await channel.close()


@pytest.mark.reliability
@pytest.mark.end_to_end
async def test_static_physics(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-01-S02：静态线性表插值结果与真实 Gateway 读回一致（y=10x）。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=TWO_FLOAT_DEVICE)
    specs: list[tuple[str, str, dict[str, Any] | None, list[tuple[str, str]]]] = [
        (
            "GW01",
            PITCH,
            {"kind": "random", "distribution": "uniform", "minimum": 0.0, "maximum": 9.0},
            [("gw-a", "GW01.pitch_angle")],
        ),
        (
            "GW01",
            RATE,
            {
                "kind": "static",
                "function": "linear_table",
                "input_point": PITCH,
                "values": [[0.0, 0.0], [10.0, 100.0]],
            },
            [("gw-a", "GW01.pitch_rate")],
        ),
    ]
    doc = simulation_document({"gw-a": gateway.data_bind}, specs, interval=0.15, seed=7)
    service_path = configs_root(scenarios=doc)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        await _start(dg_stub, revision)
        pairs: list[tuple[float, float]] = []
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline and len(pairs) < 4:
            x_sample = await read_gateway_variable(gateway.data_bind, "gw-a", "GW01.pitch_angle")
            y_sample = await read_gateway_variable(gateway.data_bind, "gw-a", "GW01.pitch_rate")
            if x_sample.quality == y_sample.quality == "GOOD":
                x_value, y_value = x_sample.value.double_value, y_sample.value.double_value
                if not pairs or abs(pairs[-1][0] - x_value) > 1e-6:
                    pairs.append((x_value, y_value))
            await asyncio.sleep(0.12)
        assert len(pairs) >= 3, pairs
        for x_value, y_value in pairs:
            assert 0.0 - 1e-6 <= x_value <= 9.0 + 1e-6
            assert y_value == pytest.approx(10.0 * x_value, abs=0.2), pairs
    finally:
        await channel.close()


@pytest.mark.reliability
@pytest.mark.fault_injection
async def test_invalid_model_and_runtime_failure(
    start_real_gateway,
    configs_root,
    dg_subprocess,
    tmp_path,  # noqa: ANN001
) -> None:
    """UC-01-S03：坏模型启动即拒；运行期非法候选不生效，活动任务与输出不被污染。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT_DEVICE)
    valid = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.2,
        mappings=[("gw-a", "GW01.pitch_angle")],
    )
    service_path = configs_root(scenarios=valid)

    # 1) 静态模型依赖环：--check 必须非零退出，进程不得报告 READY。
    cyclic = simulation_document(
        {"gw-a": gateway.data_bind},
        [
            (
                "GW01",
                PITCH,
                {
                    "kind": "static",
                    "function": "linear_table",
                    "input_point": RATE,
                    "values": [[0.0, 0.0], [1.0, 1.0]],
                },
                [("gw-a", "GW01.pitch_angle")],
            ),
            (
                "GW01",
                RATE,
                {
                    "kind": "static",
                    "function": "linear_table",
                    "input_point": PITCH,
                    "values": [[0.0, 0.0], [1.0, 1.0]],
                },
                [],
            ),
        ],
    )
    bad_service = configs_root(scenarios=cyclic, bind="127.0.0.1:9999")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_PATH) + os.pathsep + env.get("PYTHONPATH", "")
    check = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "data_generator.main",
        "--config",
        str(bad_service),
        "--check",
        cwd=str(REPO_ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await check.communicate()
    assert check.returncode != 0
    assert b"cyclic static dependency" in stdout

    # 2) 合法服务运行中，Push 含坏模型的候选必须拒绝，已生效配置与输出不变。
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        generation_id = await _start(dg_stub, revision)
        await _wait_changing_value(gateway.data_bind, "gw-a", "GW01.pitch_angle", samples=2)

        disk = await common_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
        )
        bad_document = simulation_document(
            {"gw-a": gateway.data_bind},
            [
                (
                    "GW01",
                    PITCH,
                    {
                        "kind": "static",
                        "function": "linear_table",
                        "input_point": RATE,
                        "values": [[0.0, 0.0], [1.0, 1.0]],
                    },
                    [("gw-a", "GW01.pitch_angle")],
                ),
                ("GW01", RATE, None, []),
            ],
            version="1.0.1",
        )
        files = replace_snapshot_files(
            disk,
            {_scenario_path(disk): yaml.safe_dump(bad_document, sort_keys=False).encode()},
        )
        pushed = await common_stub.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(expected_revision=revision, files=files)
        )
        assert pushed.valid is False
        assert pushed.validation[0].code in {
            "CONFIG_VALIDATION_FAILED",
            "OWNERSHIP_DENIED",
        }
        await _wait_changing_value(gateway.data_bind, "gw-a", "GW01.pitch_angle", samples=2)
        state = await dg_stub.GetGeneration(common.ObjectId(id=generation_id))
        assert state.generation.actual_state == "RUNNING"
        assert await _runtime_revision(common_stub) == revision
    finally:
        await channel.close()


@pytest.mark.performance
@pytest.mark.overload
@pytest.mark.mock_service
async def test_overrun_and_stop(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-01-S03：周期超限不重入，记录 skipped；停止后无额外输出。

    真实 Gateway 无法稳定注入处理时延，故以实现冻结正式 7-RPC 契约的故障网关
    注入 150ms 处理时间（mock_service），UpdateVariables 仍为正式接口。
    """
    catalog = {"gw-a": [("GW01.pitch_angle", "float32", True)]}
    fault = await start_fault_gateway(catalog, {"gw-a": {"delay": 0.15}})
    doc = ship_scenarios_document(
        {"gw-a": fault.bind},
        interval=0.05,
        mappings=[("gw-a", "GW01.pitch_angle")],
    )
    service_path = configs_root(scenarios=doc, output_timeout=1.0)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        generation_id = await _start(dg_stub, revision)
        await asyncio.sleep(1.2)
        metrics = metric_map(
            await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=10_000))
        )
        assert metrics["data_generator.skipped_steps_total"] >= 1
        # 每个周期最多一个批次：~1.2s/50ms 下不允许翻倍批次（不重入）。
        assert metrics["data_generator.batches_total"] <= 30
        calls_before = len(fault.service.update_calls["gw-a"])
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(generation_id=generation_id, expected_revision=revision)
        )
        assert stopped.accepted
        await asyncio.sleep(0.4)
        assert len(fault.service.update_calls["gw-a"]) == calls_before
    finally:
        await channel.close()


@pytest.mark.reliability
@pytest.mark.end_to_end
async def test_gateway_feedback(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-03-S01：仅合法 feedback 映射的变量被写入，未映射变量保持无值。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=TWO_FLOAT_DEVICE)
    doc = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.15,
        mappings=[("gw-a", "GW01.pitch_angle")],
    )
    service_path = configs_root(scenarios=doc)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        await _start(dg_stub, revision)
        await _wait_changing_value(gateway.data_bind, "gw-a", "GW01.pitch_angle", samples=2)
        for _ in range(5):
            unmapped = await read_gateway_variable(gateway.data_bind, "gw-a", "GW01.pitch_rate")
            assert unmapped.quality == "UNKNOWN"
            assert unmapped.error_code == "NO_VALUE"
            await asyncio.sleep(0.1)
    finally:
        await channel.close()


@pytest.mark.safety
@pytest.mark.invalid_data
async def test_bad_gateway_mapping(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-03-S02：编码/可写性不匹配预检拒绝启动，绝不向错误点写入。"""
    devices = {"GW01": [("bool_pt", "bool", "feedback")]}
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=devices)
    doc = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.15,
        mappings=[("gw-a", "GW01.bool_pt")],
    )
    service_path = configs_root(scenarios=doc)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        rejected = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert rejected.accepted is False
        # P2-1 整改：预检类型不匹配归类为 TARGET_VALIDATION_FAILED，消息保留细节。
        assert rejected.error_code == "TARGET_VALIDATION_FAILED"
        assert "expected writable float32" in rejected.message
        sample = await read_gateway_variable(gateway.data_bind, "gw-a", "GW01.bool_pt")
        assert sample.quality == "UNKNOWN"
    finally:
        await channel.close()


@pytest.mark.reliability
@pytest.mark.end_to_end
async def test_fanout_multiple_gateways_and_variables(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-03-S01：一源点扇出双 Gateway + Gateway A 内两变量；逐目标读回一致。"""
    gateway_a = await start_real_gateway(endpoint_id="gw-a", devices=TWO_FLOAT_DEVICE)
    gateway_b = await start_real_gateway(
        endpoint_id="gw-b",
        devices={"GW01": [("pitch_angle", "float32", "feedback")]},
    )
    specs: list[tuple[str, str, dict[str, Any] | None, list[tuple[str, str]]]] = [
        (
            "GW01",
            PITCH,
            {"kind": "random", "distribution": "uniform", "minimum": 1.0, "maximum": 8.0},
            [("gw-a", "GW01.pitch_angle"), ("gw-b", "GW01.pitch_angle")],
        ),
        (
            "GW01",
            RATE,
            {
                "kind": "static",
                "function": "linear_table",
                "input_point": PITCH,
                "values": [[0.0, 0.0], [10.0, 100.0]],
            },
            [("gw-a", "GW01.pitch_rate")],
        ),
    ]
    doc = simulation_document(
        {"gw-a": gateway_a.data_bind, "gw-b": gateway_b.data_bind},
        specs,
        interval=0.2,
        seed=11,
    )
    service_path = configs_root(scenarios=doc)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        await _start(dg_stub, revision)
        consistent = 0
        last_x = None
        deadline = asyncio.get_running_loop().time() + 12.0
        while asyncio.get_running_loop().time() < deadline and consistent < 3:
            x_a = await read_gateway_variable(gateway_a.data_bind, "gw-a", "GW01.pitch_angle")
            y_a = await read_gateway_variable(gateway_a.data_bind, "gw-a", "GW01.pitch_rate")
            x_b = await read_gateway_variable(gateway_b.data_bind, "gw-b", "GW01.pitch_angle")
            if all(sample.quality == "GOOD" for sample in (x_a, y_a, x_b)):
                xa, ya, xb = (
                    x_a.value.double_value,
                    y_a.value.double_value,
                    x_b.value.double_value,
                )
                if last_x is None or abs(last_x - xa) > 1e-6:
                    assert xa == pytest.approx(xb, abs=1e-5), (xa, xb)
                    assert ya == pytest.approx(10.0 * xa, abs=0.2), (xa, ya)
                    last_x = xa
                    consistent += 1
            await asyncio.sleep(0.12)
        assert consistent >= 3
    finally:
        await channel.close()


@pytest.mark.safety
@pytest.mark.conflict
async def test_conflicting_target_ownership(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """P1-02/P1-06：两个独立 DG 进程争同一目标，第二方拒绝；A 收敛释放后 B 可取得。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT_DEVICE)
    doc = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.3,
        mappings=[("gw-a", "GW01.pitch_angle")],
    )
    path_a = configs_root(scenarios=doc)
    path_b = configs_root(scenarios=doc)
    bind_a, _proc_a = await dg_subprocess(path_a)
    bind_b, _proc_b = await dg_subprocess(path_b)
    channel_a, dg_a, common_a = await dg_stubs(bind_a)
    channel_b, dg_b, common_b = await dg_stubs(bind_b)
    try:
        revision_a = await _runtime_revision(common_a)
        revision_b = await _runtime_revision(common_b)
        first = await dg_a.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision_a)
        )
        assert first.accepted, first.message
        await _wait_changing_value(gateway.data_bind, "gw-a", "GW01.pitch_angle", samples=1)
        second = await dg_b.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision_b)
        )
        # v1_10：Gateway 端租约仲裁，B 整组拒绝且错误码精确为 RESOURCE_BUSY。
        assert second.accepted is False
        assert second.error_code == "RESOURCE_BUSY", second.error_code
        # B 被拒后其进程仍可立即用正确修订号服务其他请求（无部分占用残留）。
        assert (await common_b.GetServiceStatus(common.Empty())).health in {
            "HEALTHY",
            "DEGRADED",
        }

        # A 停止且 I/O 收敛后租约显式释放，B 立即取得所有权（无须等 TTL）。
        stopped = await dg_a.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=first.generation_id, expected_revision=revision_a
            )
        )
        assert stopped.accepted
        state = ""
        deadline = asyncio.get_running_loop().time() + 8.0
        while asyncio.get_running_loop().time() < deadline:
            detail = await dg_a.GetGeneration(common.ObjectId(id=first.generation_id))
            state = detail.generation.actual_state
            if state == "STOPPED":
                break
            await asyncio.sleep(0.1)
        assert state == "STOPPED", state
        revision_b = await _runtime_revision(common_b)
        takeover = await dg_b.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision_b)
        )
        assert takeover.accepted, takeover.message
        await _wait_changing_value(gateway.data_bind, "gw-a", "GW01.pitch_angle", samples=1)
    finally:
        await channel_a.close()
        await channel_b.close()


@pytest.mark.safety
@pytest.mark.conflict
async def test_duplicate_target_mapping_rejected(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-03-S02：单场景两个源点映射同一目标，Push 拒绝，已生效配置不变。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=TWO_FLOAT_DEVICE)
    doc = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.2,
        mappings=[("gw-a", "GW01.pitch_angle")],
    )
    service_path = configs_root(scenarios=doc)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _runtime_revision(common_stub)
        generation_id = await _start(dg_stub, revision)
        await _wait_changing_value(gateway.data_bind, "gw-a", "GW01.pitch_angle", samples=2)

        disk = await common_stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
        )
        duplicate = simulation_document(
            {"gw-a": gateway.data_bind},
            [
                ("GW01", PITCH, None, [("gw-a", "GW01.pitch_angle")]),
                ("GW01", RATE, None, [("gw-a", "GW01.pitch_angle")]),
            ],
            version="1.0.1",
        )
        files = replace_snapshot_files(
            disk,
            {_scenario_path(disk): yaml.safe_dump(duplicate, sort_keys=False).encode()},
        )
        pushed = await common_stub.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(expected_revision=revision, files=files)
        )
        assert pushed.valid is False
        assert pushed.validation[0].code == "CONFIG_VALIDATION_FAILED"
        assert "duplicate output target" in pushed.validation[0].message
        state = await dg_stub.GetGeneration(common.ObjectId(id=generation_id))
        assert state.generation.actual_state == "RUNNING"
        assert await _runtime_revision(common_stub) == revision
    finally:
        await channel.close()
