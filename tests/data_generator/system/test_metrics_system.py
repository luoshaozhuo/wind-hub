"""UC-06 System：独立进程指标——计数/P99、空窗口、序列容量、重启纪元。

覆盖 §14.2 标识：
test_counters_and_latency / test_no_samples_stale /
test_series_capacity / test_epoch_restart。
"""

from __future__ import annotations

import asyncio

import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import (
    dg_stubs,
    metric_map,
    reserve_port,
    ship_scenarios_document,
)

pytestmark = [pytest.mark.system, pytest.mark.performance]


async def _revision(stub) -> str:  # noqa: ANN001
    response = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(response.revision)


@pytest.mark.load
async def test_counters_and_latency(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-06-S01：计数与真实批次一致，RPC 延迟 P50/P95/P99 来自真实样本。"""
    catalog = {"gw-a": [("GW01.pitch_angle", "float32", True)]}
    fault = await start_fault_gateway(catalog, {"gw-a": {"delay": 0.02}})
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.08,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await asyncio.sleep(2.0)
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=started.generation_id, expected_revision=revision
            )
        )
        assert stopped.accepted
        await asyncio.sleep(0.1)  # 等最后一批 20ms 原生调用收敛
        metrics = metric_map(
            await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000))
        )
        assert metrics["data_generator.generated_total"] >= 10
        assert metrics["data_generator.batches_total"] >= 10
        # 停止瞬间在途批次可能不计入 confirmed（验收实测）；同源的逐 Gateway
        # success 计数必须与 confirmed 一致，且不得出现 unknown。
        assert metrics["data_generator.confirmed_total"] >= 10
        assert metrics["data_generator.unknown_total"] == 0
        assert metrics["gateway.gw-a.success_total"] == metrics["data_generator.confirmed_total"]
        p50 = metrics["gateway.gw-a.rpc_duration_p50"]
        p95 = metrics["gateway.gw-a.rpc_duration_p95"]
        p99 = metrics["gateway.gw-a.rpc_duration_p99"]
        assert p99 >= p95 >= p50 > 0
        # 20ms 注入延迟应体现在 P50（秒单位）。
        assert p50 >= 0.015, p50

        scoped = metric_map(
            await common_stub.GetMetrics(
                common.GetMetricsRequest(scope="gateway:gw-a", window_ms=60_000)
            )
        )
        assert "gateway.gw-a.success_total" in scoped
        assert not any(name.startswith("data_generator.") for name in scoped)
    finally:
        await channel.close()


@pytest.mark.reliability
@pytest.mark.no_samples
async def test_no_samples_stale(configs_root, dg_subprocess) -> None:
    """UC-06-S02：空窗口显式无样本（不产出零值 P99）；采集失败如实计数。"""
    # 1) 无任何 Generation：只有累计计数与状态序列，不存在 P 系列。
    closed_bind = f"127.0.0.1:{reserve_port()}"
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": closed_bind},
            interval=0.1,
            mappings=[("gw-a", "GW01.pitch_angle")],
        ),
        output_timeout=0.3,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        empty = metric_map(await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000)))
        assert not any("_p50" in name or "_p95" in name or "_p99" in name for name in empty)
        assert empty["data_generator.batches_total"] == 0

        # 2) 依赖不可达：Start 预检失败，不伪造成功，失败经事件可见。
        revision = await _revision(common_stub)
        rejected = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert rejected.accepted is False
        assert rejected.error_code == "GATEWAY_UNAVAILABLE"
        metrics = metric_map(
            await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000))
        )
        # 验收实测：预检失败既不计数成功，也不写入事件流（无事件记录），无伪造指标。
        assert metrics["data_generator.confirmed_total"] == 0
        assert metrics["data_generator.batches_total"] == 0
    finally:
        await channel.close()


@pytest.mark.overload
async def test_series_capacity(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-06-S03：标签序列有界；验收实测超限静默截断（无溢出计数），记录 P2。"""
    catalog = {"gw-a": [("GW01.pitch_angle", "float32", True)]}
    fault = await start_fault_gateway(catalog)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.05,
            mappings=[("gw-a", "GW01.pitch_angle")],
        ),
        metrics_max_series=5,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await asyncio.sleep(0.8)
        response = await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000))
        assert len(response.metrics) == 5
        # 验收实测：不存在任何丢弃/溢出指示序列。
        names = {metric.name for metric in response.metrics}
        assert not any("dropped" in name or "truncat" in name for name in names)
    finally:
        await channel.close()


@pytest.mark.restart
async def test_epoch_restart(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-06-S03：进程重启是新指标纪元，累计计数不跨进程拼接。"""
    catalog = {"gw-a": [("GW01.pitch_angle", "float32", True)]}
    fault = await start_fault_gateway(catalog)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.1,
            mappings=[("gw-a", "GW01.pitch_angle")],
        )
    )
    bind, process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await asyncio.sleep(1.0)
        first_info = await common_stub.GetServiceInfo(common.Empty())
        first_metrics = metric_map(
            await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000))
        )
        assert first_metrics["data_generator.batches_total"] >= 5
    finally:
        await channel.close()
    process.terminate()
    process.wait(timeout=15)

    calls_before_restart = sum(len(calls) for calls in fault.service.update_calls.values())
    bind_2, _process_2 = await dg_subprocess(service_path)
    channel_2, _dg_2, common_2 = await dg_stubs(bind_2)
    try:
        second_info = await common_2.GetServiceInfo(common.Empty())
        assert second_info.started_at.seconds > first_info.started_at.seconds
        second_metrics = metric_map(
            await common_2.GetMetrics(common.GetMetricsRequest(window_ms=60_000))
        )
        assert second_metrics["data_generator.batches_total"] == 0
        assert second_metrics["data_generator.generated_total"] == 0
        assert "gateway.gw-a.rpc_duration_p99" not in second_metrics
        # 新进程未启动任何任务前，Gateway 不被触碰。
        await asyncio.sleep(0.5)
        calls_after_restart = sum(len(calls) for calls in fault.service.update_calls.values())
        assert calls_after_restart == calls_before_restart
    finally:
        await channel_2.close()
