"""UC-03 组件级：在途容量、Gateway 串行、UNKNOWN 隔离与背压。

无法用真实 Gateway 稳定制造挂起/超时，故故障注入器实现**冻结的正式
IndustrialGatewayService 契约**（与生产同一份 proto，不引入旧接口），
仅用于组件层背压/超时语义；成功路径由 integration/system 层真实 Gateway 证明。
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from data_generator.domain.model import DataBatch, DataPoint, OutputTarget
from data_generator.domain.scenario import Scenario
from data_generator.infrastructure.adapters.outbound.gateway import GatewayWriter
from industrial_gateway.application.target_leases import TargetLeaseRegistry

pytestmark = [
    pytest.mark.component,
    pytest.mark.performance,
    pytest.mark.overload,
    pytest.mark.mock_service,
]


class FaultGatewayService(gw_rpc.IndustrialGatewayServiceServicer):
    def __init__(
        self,
        *,
        delay: float = 0.0,
        gate: asyncio.Event | None = None,
        per_item_fail: frozenset[str] = frozenset(),
        omit: frozenset[str] = frozenset(),
        abort: grpc.StatusCode | None = None,
    ) -> None:
        self.delay = delay
        self.gate = gate
        self.per_item_fail = per_item_fail
        self.omit = omit
        self.abort = abort
        self.calls: list[list[str]] = []
        self.leases = TargetLeaseRegistry()

    async def GetGateway(self, request, context):  # noqa: ANN001, ANN202, N802
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "not used by data generator")

    async def StartGateway(self, request, context):  # noqa: ANN001, ANN202, N802
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "not used by data generator")

    async def StopGateway(self, request, context):  # noqa: ANN001, ANN202, N802
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "not used by data generator")

    async def ListGateways(self, request, context):  # noqa: ANN001, ANN202, N802
        return gw.ListGatewaysResponse(
            gateways=[gw.Gateway(gateway_id="gw-a", actual_state="RUNNING")]
        )

    async def ClaimTargets(self, request, context):  # noqa: ANN001, ANN202, N802
        ids = list(request.variable_ids)
        async with self.leases.lock:
            accepted, token, error = self.leases.claim(
                request.gateway_id,
                request.owner_id,
                set(ids),
                request.action,
                request.lease_token,
                request.lease_ms,
            )
        return gw.ClaimTargetsResponse(accepted=accepted, lease_token=token, error_code=error)

    async def ListVariables(self, request, context):  # noqa: ANN001, N802
        return gw.ListVariablesResponse(
            variables=[
                gw.Variable(
                    variable_id="GW01.a", name="GW01.a", data_type="float32", writable=True
                ),
                gw.Variable(
                    variable_id="GW01.b", name="GW01.b", data_type="float32", writable=True
                ),
            ]
        )

    async def UpdateVariables(self, request, context):  # noqa: ANN001, N802
        ids = [item.variable_id for item in request.values]
        async with self.leases.lock:
            if not self.leases.authorize(request.gateway_id, set(ids), request.lease_token):
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "LEASE_LOST")
        self.calls.append(ids)
        if self.abort is not None:
            await context.abort(self.abort, "injected")
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        results = [
            gw.VariableUpdateResult(
                variable_id=item.variable_id,
                success=item.variable_id not in self.per_item_fail,
                error_code="INJECTED" if item.variable_id in self.per_item_fail else "",
            )
            for item in request.values
            if item.variable_id not in self.omit
        ]
        return gw.UpdateVariablesResponse(results=results)

    async def ReadVariables(self, request, context):  # noqa: ANN001, ANN202, N802
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "not used by data generator")


def _scenario(bind: str) -> Scenario:
    from data_generator.domain.model import OutputMapping

    return Scenario(
        scenario_id="s",
        version="1.0.0",
        description="",
        source={"kind": "simulation", "interval_seconds": 1.0, "seed": 1},
        devices=("GW01",),
        points={
            ("GW01", "a"): {"data_type": "float32"},
            ("GW01", "b"): {"data_type": "float32"},
        },
        mappings=(
            OutputMapping("GW01", "a", OutputTarget("g1", "GW01.a")),
            OutputMapping("GW01", "b", OutputTarget("g1", "GW01.b")),
        ),
        gateways={"g1": bind},
    )


def _batch() -> DataBatch:
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    return DataBatch(
        "gen",
        1,
        now,
        (DataPoint("GW01", "a", 1.0, now), DataPoint("GW01", "b", 2.0, now)),
    )


async def _start_service(
    service: gw_rpc.IndustrialGatewayServiceServicer,
) -> tuple[grpc.aio.Server, str]:
    server = grpc.aio.server()
    gw_rpc.add_IndustrialGatewayServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, f"127.0.0.1:{port}"


async def test_logical_timeout_is_unknown_and_quarantines_gateway() -> None:
    server, bind = await _start_service(FaultGatewayService(delay=2.0))
    writer = GatewayWriter(timeout=0.3, max_inflight=4)
    try:
        scenario = _scenario(bind)
        await writer.validate(scenario)
        await writer.claim(scenario, "gen-1")
        results = await writer.send(scenario, _batch())
        assert {result.state for result in results} == {"UNKNOWN"}
        assert all(result.error_code == "RPC_TIMEOUT" for result in results)
        assert "g1" in writer.unsafe_gateways
        # 隔离后不得盲目重发：直接返回 TARGET_QUARANTINED。
        results_again = await writer.send(scenario, _batch())
        assert {result.error_code for result in results_again} == {"TARGET_QUARANTINED"}
    finally:
        await writer.release(scenario, "gen-1")
        await server.stop(grace=0)
        await writer.close()


async def test_clear_rejection_is_failed_without_quarantine() -> None:
    server, bind = await _start_service(
        FaultGatewayService(abort=grpc.StatusCode.PERMISSION_DENIED)
    )
    writer = GatewayWriter(timeout=1.0, max_inflight=4)
    try:
        scenario = _scenario(bind)
        await writer.validate(scenario)
        await writer.claim(scenario, "gen-1")
        results = await writer.send(scenario, _batch())
        assert {result.state for result in results} == {"FAILED"}
        assert "g1" not in writer.unsafe_gateways
    finally:
        await writer.release(scenario, "gen-1")
        await server.stop(grace=0)
        await writer.close()


async def test_per_item_failure_and_missing_ack_classification() -> None:
    server, bind = await _start_service(FaultGatewayService(per_item_fail=frozenset({"GW01.b"})))
    writer = GatewayWriter(timeout=1.0, max_inflight=4)
    try:
        scenario = _scenario(bind)
        await writer.validate(scenario)
        await writer.claim(scenario, "gen-1")
        results = await writer.send(scenario, _batch())
        states = {result.target.variable_id: result.state for result in results}
        assert states == {"GW01.a": "SUCCESS", "GW01.b": "FAILED"}
    finally:
        await writer.release(scenario, "gen-1")
        await server.stop(grace=0)
        await writer.close()

    server, bind = await _start_service(FaultGatewayService(omit=frozenset({"GW01.b"})))
    writer = GatewayWriter(timeout=1.0, max_inflight=4)
    try:
        scenario = _scenario(bind)
        await writer.validate(scenario)
        await writer.claim(scenario, "gen-1")
        results = await writer.send(scenario, _batch())
        # 缺 ACK 属于不可信应答：整批保守标记 UNKNOWN/INVALID_ACK，并隔离 Gateway。
        assert {result.state for result in results} == {"UNKNOWN"}
        assert all(result.error_code == "INVALID_ACK" for result in results)
        assert "g1" in writer.unsafe_gateways
    finally:
        await writer.release(scenario, "gen-1")
        await server.stop(grace=0)
        await writer.close()


async def test_global_inflight_saturation_causes_bounded_backpressure() -> None:
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()
    server_a, bind_a = await _start_service(FaultGatewayService(gate=gate_a))
    server_b, bind_b = await _start_service(FaultGatewayService(gate=gate_b))
    # 全局许可=1，逻辑超时 0.3s：g1 在途占住全局信号量，g2 必须有界失败而非堆积。
    writer = GatewayWriter(timeout=0.3, max_inflight=1)
    from data_generator.domain.model import OutputMapping

    scenario = Scenario(
        scenario_id="s",
        version="1.0.0",
        description="",
        source={"kind": "simulation", "interval_seconds": 1.0, "seed": 1},
        devices=("GW01",),
        points={
            ("GW01", "a"): {"data_type": "float32"},
            ("GW01", "b"): {"data_type": "float32"},
        },
        mappings=(
            OutputMapping("GW01", "a", OutputTarget("g1", "GW01.a")),
            OutputMapping("GW01", "b", OutputTarget("g2", "GW01.b")),
        ),
        gateways={"g1": bind_a, "g2": bind_b},
    )
    try:
        await writer.validate(scenario)
        await writer.claim(scenario, "gen-1")
        first = asyncio.ensure_future(writer.send(scenario, _batch()))
        await asyncio.sleep(0.15)
        second = await writer.send(scenario, _batch())
        # g2 锁空闲但全局许可被 g1 占住：等待有界后 BACKPRESSURE_TIMEOUT。
        g2 = [r for r in second if r.target.gateway_id == "g2"]
        assert {result.error_code for result in g2} == {"BACKPRESSURE_TIMEOUT"}
        gate_a.set()
        gate_b.set()
        results = await first
        assert {result.target.gateway_id for result in results} == {"g1", "g2"}
    finally:
        gate_a.set()
        gate_b.set()
        await writer.release(scenario, "gen-1")
        await server_a.stop(grace=0)
        await server_b.stop(grace=0)
        await writer.close()
