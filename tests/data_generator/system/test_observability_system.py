"""OBS-1/OBS-2 系统复测：UNKNOWN 隔离/恢复不重放 与 派发/原生两层指标口径。

真实独立 DG 进程 + 冻结契约故障网关（唯一可稳定构造挂起/超时的手段）。
关键口径：
- data_generator.batches_total        = 来源批次/派发尝试（含背压拒绝）
- gateway.<id>.rpc_native_started_total = 真实发起原生 UpdateVariables 任务数
两者在挂起窗口必然分叉；原生数必须与服务端实际收到的调用数逐一对账。
"""

from __future__ import annotations

import asyncio

import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import FaultGatewayService, dg_stubs, metric_map, ship_scenarios_document

pytestmark = [pytest.mark.system, pytest.mark.overload]

VARIABLE = "GW01.pitch_angle"


async def _revision(common_stub) -> str:  # noqa: ANN001
    response = await common_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(response.revision)


async def _metrics(common_stub) -> dict[str, float]:  # noqa: ANN001
    return metric_map(await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=60_000)))


async def _wait_first_call(fault, gateway_id: str = "gw-a") -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        if fault.service.update_calls.get(gateway_id):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no native UpdateVariables call observed")


async def test_obs2_native_started_matches_actual_rpc_not_dispatch(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """挂起窗口：派发尝试（batches_total）增长，真实原生数有界且与服务端对账一致。"""
    FaultGatewayService.reset_concurrency()
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate}}
    )
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.05,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=1.0,
        max_inflight=2,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await _wait_first_call(fault)
        await asyncio.sleep(1.6)  # 两个在途许可耗尽后，后续批次走背压拒绝，绝不发起原生调用

        native_observed = len(fault.service.update_calls["gw-a"])
        metrics = await _metrics(common_stub)
        # 单网关串行派发：阻塞窗口内只有 1 个真实原生调用（与背压系统测试一致），
        # 全局在途峰值仍受 max_inflight=2 上界约束；多余源节拍走跳过/隔离，不虚增原生数。
        assert native_observed == 1, native_observed
        assert FaultGatewayService.global_max_active <= 2
        assert metrics["gateway.gw-a.rpc_native_started_total"] == native_observed
        assert metrics["data_generator.batches_total"] > native_observed
        assert metrics["data_generator.skipped_steps_total"] >= 1

        # 恢复：后续新批次正常确认；被隔离的 UNKNOWN 批次绝不重放。
        gate.set()
        # 恢复由 1/3 租期的受控 RENEW 探测驱动（lease=12s => 周期 4s）：轮询至确认恢复。
        deadline = asyncio.get_running_loop().time() + 12.0
        while asyncio.get_running_loop().time() < deadline:
            metrics = await _metrics(common_stub)
            if metrics["data_generator.confirmed_total"] >= 3:
                break
            await asyncio.sleep(0.2)
        await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=started.generation_id, expected_revision=revision
            )
        )
        await asyncio.sleep(0.2)
        metrics = await _metrics(common_stub)
        # 指标口径与真实 RPC 数始终一致（恢复后新增的都是新批次，无旧批次重放）。
        assert metrics["gateway.gw-a.rpc_native_started_total"] == len(
            fault.service.update_calls["gw-a"]
        )
        assert metrics["data_generator.confirmed_total"] >= 3
        assert metrics["gateway.gw-a.success_total"] == metrics["data_generator.confirmed_total"]
    finally:
        await channel.close()


async def test_obs1_unknown_quarantine_no_resend_and_recovery_probe(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """OBS-1：逻辑超时只计 UNKNOWN；隔离期不重发；受控续租探测恢复后不重复/不越权。"""
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate}}
    )
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.1,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=0.4,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await _wait_first_call(fault)
        await asyncio.sleep(1.4)
        # 隔离期：只有 1 个真实原生调用；UNKNOWN 已计数；没有任何盲重发。
        assert len(fault.service.update_calls["gw-a"]) == 1
        metrics = await _metrics(common_stub)
        assert metrics["data_generator.unknown_total"] >= 1
        unknown_frozen = metrics["data_generator.unknown_total"]

        # 恢复：续租探测成功 + 无在途原生调用 => 解除隔离；之后只有新批次发起 RPC。
        gate.set()
        deadline = asyncio.get_running_loop().time() + 8.0
        while asyncio.get_running_loop().time() < deadline:
            if (await common_stub.GetServiceStatus(common.Empty())).health == "HEALTHY":
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.8)
        await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=started.generation_id, expected_revision=revision
            )
        )
        metrics = await _metrics(common_stub)
        assert len(fault.service.update_calls["gw-a"]) >= 3  # 恢复后有新批次
        assert metrics["data_generator.unknown_total"] == unknown_frozen  # 旧批次未重放
        assert metrics["data_generator.confirmed_total"] >= 2
        # 服务端每次写入都带当前有效租约令牌（围栏在 FaultGatewayService 内强制执行），
        # 因此到达 update_calls 的调用不可能越权；无多目标 => 无跨目标写入。
        assert all(call == [VARIABLE] for call in fault.service.update_calls["gw-a"])
    finally:
        await channel.close()
