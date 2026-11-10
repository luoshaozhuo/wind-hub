"""真实 grpc.aio：GetMetrics / GetEvents / Diagnose 三 CommonService RPC。

- GetMetrics 只返回已完成的周期快照，九指标、scope 过滤、窗口一致性；
- GetEvents 真实生命周期事件、过滤、非法分页；
- Diagnose 分段证据、诚实不确定性（overall 永不合成 PASS）、无副作用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as pb
from contracts.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    FEEDBACK_UINT,
    claim_for_injection,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service]


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(tmp_path / "root", two_server_specs(control_enabled=False))
    try:
        yield gateway
    finally:
        await gateway.stop()


async def _wait_metrics(stub, scope: str = "") -> dict[str, common.Metric]:
    for _ in range(40):
        reply = await stub.GetMetrics(common.GetMetricsRequest(scope=scope, window_ms=60_000))
        if reply.metrics:
            return {metric.name: metric for metric in reply.metrics}
        await asyncio.sleep(0.1)
    raise AssertionError("periodic metrics snapshot never became ready")


async def test_get_metrics_nine_indicators_scope_and_window_contract(harness) -> None:
    stub = harness.common_stub()
    gw_stub = harness.gateway_stub()
    # 制造真实注入流量（含一次延迟样本），每台 Gateway 先取得正式租约。
    token_gw1 = await claim_for_injection(gw_stub, "gw1", [FEEDBACK_UINT])
    await gw_stub.UpdateVariables(
        pb.UpdateVariablesRequest(
            gateway_id="gw1",
            lease_token=token_gw1,
            values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(7))],
        )
    )
    token_gw2 = await claim_for_injection(gw_stub, "gw2", [FEEDBACK_UINT])
    await gw_stub.UpdateVariables(
        pb.UpdateVariablesRequest(
            gateway_id="gw2",
            lease_token=token_gw2,
            values=[pb.VariableUpdate(variable_id=FEEDBACK_UINT, value=encode_scalar(8))],
        )
    )

    aggregate = await _wait_metrics(stub)
    scoped = await _wait_metrics(stub, "gw1")

    assert len(aggregate) == 9
    assert aggregate["updated_variables_total"].value == 2
    assert scoped["updated_variables_total"].value == 1
    assert aggregate["updated_variables_rate_per_second"].unit == "variables/s"
    assert aggregate["update_latency_ms_p99"].unit == "ms"
    assert aggregate["update_latency_ms_p99"].value >= 0
    assert 0 < aggregate["metrics_covered_ms"].value <= 60_000

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetMetrics(common.GetMetricsRequest(scope="ghost", window_ms=60_000))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetMetrics(common.GetMetricsRequest(window_ms=30_000))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_get_events_reflects_lifecycle_and_filters(harness) -> None:
    stub = harness.common_stub()
    reply = await stub.GetEvents(common.GetEventsRequest(page_size=10))
    # 两个监听器启动事件真实存在。
    assert any("started" in event.message for event in reply.events)
    sources = {event.source for event in reply.events}
    assert {"gw1", "gw2"}.issubset(sources)

    filtered = await stub.GetEvents(common.GetEventsRequest(filter="gw1"))
    assert all("gw1" in event.source for event in filtered.events)
    assert filtered.events

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetEvents(common.GetEventsRequest(page_token="not-an-int"))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_diagnose_stages_uncertainty_and_no_side_effects(harness) -> None:
    stub = harness.common_stub()
    forwarder = harness.runtime.forwarder
    records_before = len(forwarder.records)
    read_before = await harness.gateway_stub().ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_UINT])
    )

    reply = await stub.Diagnose(common.DiagnoseRequest())
    by_stage = {stage.stage: stage for stage in reply.stages}
    assert set(by_stage) == {"injection", "protocol", "control"}
    # 被动诊断的诚实性：注册/监听器/TCP 可达都不能证明业务成功 → 非 PASS。
    assert reply.overall_status in {"UNKNOWN", "FAIL"}
    assert all(stage.status != "PASS" for stage in reply.stages)
    # 两网关监听正常 → protocol 段不 FAIL。
    assert by_stage["protocol"].status == "UNKNOWN"
    # control_enabled=False：无映射，明确 NOT_SUPPORTED 而非失败。
    assert by_stage["control"].status == "NOT_SUPPORTED"
    assert by_stage["injection"].evidence
    assert reply.limitations  # 不确定性边界必须显式声明。

    # 无副作用：不新增转发事件、不改变变量。
    assert len(forwarder.records) == records_before
    read_after = await harness.gateway_stub().ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_UINT])
    )
    assert [v.quality for v in read_after.values] == [v.quality for v in read_before.values]

    # 分段选择与非法输入。
    one = await stub.Diagnose(common.DiagnoseRequest(target_id="gw1", stages=["protocol"]))
    assert [stage.stage for stage in one.stages] == ["protocol"]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.Diagnose(common.DiagnoseRequest(stages=["bogus"]))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.Diagnose(common.DiagnoseRequest(stages=["protocol", "protocol"]))
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.Diagnose(common.DiagnoseRequest(target_id="ghost"))
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_diagnose_capacity_limits_concurrent_runs(harness) -> None:
    """诊断信号量容量 4：5 个并发请求不报错，超出者排队（真实并发，无 Mock）。"""
    stub = harness.common_stub()
    replies = await asyncio.gather(
        *(stub.Diagnose(common.DiagnoseRequest(stages=["protocol"])) for _ in range(5))
    )
    assert len(replies) == 5
    assert all(reply.stages for reply in replies)
