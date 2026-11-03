"""UC-05 system tests: staged Diagnose evidence, statuses and limitations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from contracts import common_pb2 as common
from contracts.common_pb2 import DiagnoseResponse
from contracts.common_pb2_grpc import CommonServiceStub
from tests.collector.system.conftest import ADMIN_METADATA, ClientsFactory
from tests.collector.system.helpers import MODBUS, start_task, wait_actual
from tests.fixtures.servers.modbus_server import ModbusMockServer

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


async def _diagnose(
    config_stub: CommonServiceStub,
    *,
    target_id: str = "",
    stages: list[str] | None = None,
    timeout_ms: int | None = None,
) -> DiagnoseResponse:
    kwargs: dict[str, Any] = {}
    if target_id:
        kwargs["target_id"] = target_id
    if stages is not None:
        kwargs["stages"] = stages
    if timeout_ms is not None:
        kwargs["timeout_ms"] = timeout_ms
    response: DiagnoseResponse = await config_stub.Diagnose(
        common.DiagnoseRequest(**kwargs),
        metadata=ADMIN_METADATA,
    )
    return response


async def test_uc05_s01_selected_stages_carry_evidence(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"

    response = await _diagnose(config_stub, target_id=MODBUS, stages=["NETWORK", "PROTOCOL"])
    assert {stage.stage: stage.status for stage in response.stages} == {
        "NETWORK": "PASS",
        "PROTOCOL": "PASS",
    }
    network = next(stage for stage in response.stages if stage.stage == "NETWORK")
    assert {item.name: item.value for item in network.evidence}["port"].isdigit()
    assert all(stage.observed_at.ToNanoseconds() for stage in response.stages)
    assert response.overall_status == "PASS"


async def test_uc05_s02_no_target_unknown_stage_and_boundary_limitation(
    collector: Callable[..., Any], grpc_clients: ClientsFactory, real_nats: object
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    # The publisher transport connects lazily on the first sample.
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    response = await _diagnose(config_stub, stages=["NATS", "BOUNDARY", "BLACK_MAGIC"])
    statuses = {stage.stage: stage.status for stage in response.stages}
    assert statuses == {"NATS": "PASS", "BOUNDARY": "PASS", "BLACK_MAGIC": "NOT_RUN"}
    assert response.overall_status == "UNKNOWN"
    limitations = "\n".join(response.limitations)
    assert "No target device" in limitations
    assert "Sink" in limitations
    # Device-chain stages without a target are explicitly not evaluated.
    untargeted = await _diagnose(config_stub, target_id="", stages=["NETWORK"])
    assert untargeted.stages[0].status == "NOT_RUN"
    assert "no available device target" in untargeted.stages[0].message


async def test_uc05_s03_unreachable_endpoint_fails_and_deadline_stops_later_stages(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    modbus_slave: ModbusMockServer,
) -> None:
    import asyncio

    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    await modbus_slave.stop()
    try:
        await asyncio.sleep(0.5)
        failed = await _diagnose(config_stub, target_id=MODBUS, stages=["NETWORK"])
        assert failed.stages[0].status == "FAIL"
        assert failed.overall_status == "FAIL"
        # Stopping the local listener refuses the TCP connection: the stage
        # must distinguish the closed port from a routing/NETWORK failure.
        assert failed.stages[0].error_code == "PORT"

    finally:
        await modbus_slave.start()


async def test_uc05_s03_expired_deadline_marks_later_stages_not_run(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    import asyncio

    from tests.collector.system.blackhole import TcpBlackhole

    hole = TcpBlackhole()
    await hole.start()
    try:
        handle = await collector(blackhole_port=hole.port)
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
        await start_task(device_stub, config_stub, "modbus-hold")
        await asyncio.sleep(1.0)  # TCP session establishes; no protocol reply ever

        deadline = await _diagnose(
            config_stub,
            target_id="modbus-hold",
            stages=["NETWORK", "PROTOCOL", "ACQUISITION"],
            timeout_ms=1,
        )
        # A 1 ms budget is consumed by the first probe; whether the TCP
        # handshake just fits (PASS) or is itself cut short (NOT_RUN), a
        # budget cut must never be reported as a network FAIL, and remaining
        # stages honestly say NOT_RUN instead of pretending they ran.
        assert deadline.stages[0].status in {"PASS", "NOT_RUN"}
        assert deadline.stages[1].status == "NOT_RUN"
        assert "deadline" in deadline.stages[1].message
        assert deadline.stages[2].status == "NOT_RUN"
        assert deadline.overall_status == "UNKNOWN"
    finally:
        await hole.stop()
