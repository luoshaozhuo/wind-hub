"""Production GatewayWriter against a real Bearer-authorized gRPC server."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import grpc
import pytest

from contracts import common_pb2 as common
from data_generator.domain.model import (
    DataBatch,
    DataPoint,
    GeneratorError,
    OutputMapping,
    OutputTarget,
)
from data_generator.domain.scenario import Scenario
from data_generator.infrastructure.adapters.inbound.diagnostics import Diagnostics
from data_generator.infrastructure.adapters.outbound.gateway import GatewayWriter
from data_generator.infrastructure.config.service_settings import load_settings
from tests.support.bearer import ensure_bearer
from tests.support.gateway import (
    FEEDBACK_UINT,
    GatewayHarness,
    start_gateway,
    two_server_specs,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

RIGHTS = {
    "generator": ["read", "inject"],
    "gw-admin": ["read", "config_read", "config_write", "inject", "lifecycle", "diagnose"],
    "unauthorized-generator": ["inject"],
}
STRANGER = "unauthorized-generator"


@pytest.fixture
async def gateway(tmp_path: Path):
    running = await start_gateway(
        tmp_path / "root",
        two_server_specs(control_enabled=False),
        mtls=RIGHTS,
        bootstrap_cn="gw-admin",
        extra_clients=[STRANGER, "unregistered-generator"],
    )
    try:
        yield running
    finally:
        await running.stop()


def _scenario(gateway: GatewayHarness) -> Scenario:
    return Scenario(
        scenario_id="bearer",
        version="1",
        description="real Bearer writer chain",
        source={},
        devices=("D01",),
        points={("D01", "power"): {"data_type": "uint16"}},
        mappings=(OutputMapping("D01", "power", OutputTarget("gw1", FEEDBACK_UINT)),),
        gateways={"gw1": f"127.0.0.1:{gateway.grpc_port}"},
    )


def _batch(sequence: int, value: int) -> DataBatch:
    now = datetime.now(UTC)
    return DataBatch("generator", sequence, now, (DataPoint("D01", "power", value, now),))


async def test_writer_bearer_full_lease_update_and_channel_reuse(gateway: GatewayHarness) -> None:
    scenario = _scenario(gateway)
    writer = GatewayWriter(ensure_bearer("generator"), timeout=1)
    try:
        await writer.validate(scenario)
        endpoint = scenario.gateways["gw1"]
        channel = writer._rpc._channels[endpoint]
        assert isinstance(channel, grpc.aio.Channel)

        await writer.claim(scenario, "generator-owner")
        assert writer._rpc._channels[endpoint] is channel
        first = await writer.send(scenario, _batch(1, 41))
        assert [result.state for result in first] == ["SUCCESS"]

        # The production renewer must keep this lease valid beyond its original
        # ten-second deadline while reusing the same authenticated channel.
        await asyncio.sleep(11.0)
        assert "gw1" not in writer._lost_leases
        second = await writer.send(scenario, _batch(2, 42))
        assert [result.state for result in second] == ["SUCCESS"]
        assert writer._rpc._channels[endpoint] is channel

        assert await writer.release(scenario, "generator-owner")
        assert not writer.has_claims("generator-owner")
    finally:
        assert await writer.close()


async def test_writer_rejects_unauthorized_bearer_identity(gateway: GatewayHarness) -> None:
    writer = GatewayWriter(ensure_bearer(STRANGER), timeout=2)
    try:
        with pytest.raises(GeneratorError, match="variable preflight failed"):
            await writer.validate(_scenario(gateway))
    finally:
        await writer.close()


async def test_writer_rejects_unregistered_bearer_identity(gateway: GatewayHarness) -> None:
    writer = GatewayWriter(ensure_bearer("unregistered-generator"), timeout=2)
    try:
        with pytest.raises(GeneratorError, match="variable preflight failed"):
            await writer.validate(_scenario(gateway))
    finally:
        await writer.close()


def _diagnostics(gateway: GatewayHarness, token_ref: str) -> Diagnostics:
    settings = load_settings(
        Path(__file__).parents[3] / "configs/services/data-generator-sim.example.yaml"
    )
    output = dict(settings.service["output"])
    output["gateway_token_ref"] = token_ref
    configured = replace(
        settings,
        service={
            **settings.service,
            "output": output,
            "diagnose": {"timeout_seconds": 2.0},
        },
        scenarios={"bearer": _scenario(gateway)},
    )
    return Diagnostics(configured)


async def _diagnose_grpc(diagnostics: Diagnostics) -> common.DiagnoseResponse:
    return await diagnostics.run(
        common.DiagnoseRequest(target_id="bearer", stages=["grpc"], timeout_ms=2000)
    )


async def test_diagnostics_uses_production_bearer_credentials(
    gateway: GatewayHarness,
) -> None:
    response = await _diagnose_grpc(_diagnostics(gateway, ensure_bearer("generator")))
    assert response.overall_status == "PASS", response.stages[0].message
    evidence = {item.name: item.value for item in response.stages[0].evidence}
    assert evidence == {"gw1.validated_variables": "1"}


@pytest.mark.parametrize("identity", [STRANGER, "unregistered-generator"])
async def test_diagnostics_rejects_unauthorized_or_unregistered_identity(
    gateway: GatewayHarness, identity: str
) -> None:
    response = await _diagnose_grpc(_diagnostics(gateway, ensure_bearer(identity)))
    assert response.overall_status == "FAIL"
    assert response.stages[0].error_code == "DIAGNOSTIC_FAILED"
    assert response.stages[0].evidence == []


def test_writer_requires_bearer_token() -> None:
    writer = GatewayWriter("")
    with pytest.raises(ValueError, match="bearer token source"):
        writer._rpc._stub("127.0.0.1:50051")
    assert writer._rpc._channels == {}
