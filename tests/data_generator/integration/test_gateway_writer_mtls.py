"""Production GatewayWriter against a real certificate-enforcing gRPC server."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

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
from tests.support.gateway import (
    FEEDBACK_UINT,
    GatewayHarness,
    start_gateway,
    two_server_specs,
    write_mtls_pki,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

RIGHTS = {
    "generator": ["read", "inject"],
    "gw-admin": ["read", "config_read", "config_write", "inject", "lifecycle", "diagnose"],
}
STRANGER = "unauthorized-generator"


@pytest.fixture
async def gateway(tmp_path: Path):
    running = await start_gateway(
        tmp_path / "root",
        two_server_specs(control_enabled=False),
        mtls=RIGHTS,
        bootstrap_cn="gw-admin",
        extra_clients=[STRANGER],
    )
    try:
        yield running
    finally:
        await running.stop()


def _tls(gateway: GatewayHarness, client: str, *, ca_file: Path | None = None) -> dict[str, str]:
    assert gateway.pki is not None
    cert, key = cast(dict[str, tuple[Path, Path]], gateway.pki["clients"])[client]
    return {
        "ca_file": str(ca_file or cast(Path, gateway.pki["ca"])),
        "cert_file": str(cert),
        "key_file": str(key),
    }


def _scenario(gateway: GatewayHarness) -> Scenario:
    return Scenario(
        scenario_id="mtls",
        version="1",
        description="real mTLS writer chain",
        source={},
        devices=("D01",),
        points={("D01", "power"): {"data_type": "uint16"}},
        mappings=(OutputMapping("D01", "power", OutputTarget("gw1", FEEDBACK_UINT)),),
        gateways={"gw1": f"127.0.0.1:{gateway.grpc_port}"},
    )


def _batch(sequence: int, value: int) -> DataBatch:
    now = datetime.now(UTC)
    return DataBatch("generator", sequence, now, (DataPoint("D01", "power", value, now),))


async def test_writer_mtls_full_lease_update_and_channel_reuse(gateway: GatewayHarness) -> None:
    scenario = _scenario(gateway)
    writer = GatewayWriter(timeout=1, gateway_tls=_tls(gateway, "generator"))
    try:
        await writer.validate(scenario)
        endpoint = scenario.gateways["gw1"]
        channel = writer._rpc._channels[endpoint]
        assert isinstance(channel, grpc.aio.Channel)

        await writer.claim(scenario, "generator-owner")
        assert writer._rpc._channels[endpoint] is channel
        first = await writer.send(scenario, _batch(1, 41))
        assert [result.state for result in first] == ["SUCCESS"]

        # With timeout=1 the initial lease is 10 seconds and the production
        # renewer runs every lease_ms / 3. Update after the original lease would
        # have expired: success therefore requires a real RENEW on this channel.
        await asyncio.sleep(11.0)
        assert "gw1" not in writer._lost_leases
        second = await writer.send(scenario, _batch(2, 42))
        assert [result.state for result in second] == ["SUCCESS"]
        assert writer._rpc._channels[endpoint] is channel

        assert await writer.release(scenario, "generator-owner")
        assert not writer.has_claims("generator-owner")
    finally:
        assert await writer.close()


async def test_writer_rejects_unauthorized_certificate_identity(gateway: GatewayHarness) -> None:
    writer = GatewayWriter(timeout=2, gateway_tls=_tls(gateway, STRANGER))
    try:
        with pytest.raises(GeneratorError, match="variable preflight failed"):
            await writer.validate(_scenario(gateway))
    finally:
        await writer.close()


async def test_writer_rejects_wrong_ca_and_plaintext(
    gateway: GatewayHarness, tmp_path: Path
) -> None:
    other_pki = write_mtls_pki(tmp_path / "other-pki", ())
    wrong_ca = GatewayWriter(
        timeout=2,
        gateway_tls=_tls(gateway, "generator", ca_file=cast(Path, other_pki["ca"])),
    )
    plaintext = GatewayWriter(timeout=2)
    try:
        with pytest.raises(GeneratorError, match="variable preflight failed"):
            await wrong_ca.validate(_scenario(gateway))
        with pytest.raises(GeneratorError, match="variable preflight failed"):
            await plaintext.validate(_scenario(gateway))
    finally:
        await wrong_ca.close()
        await plaintext.close()


def _diagnostics(gateway: GatewayHarness, tls: dict[str, str] | None) -> Diagnostics:
    settings = load_settings(
        Path(__file__).parents[3] / "configs/services/service_data-generator-sim_v1.0.3.yaml"
    )
    output = dict(settings.service["output"])
    output["gateway_tls"] = tls
    configured = replace(
        settings,
        service={
            **settings.service,
            "output": output,
            "diagnose": {"timeout_seconds": 2.0},
        },
        scenarios={"mtls": _scenario(gateway)},
    )
    return Diagnostics(configured)


async def _diagnose_grpc(diagnostics: Diagnostics) -> common.DiagnoseResponse:
    return await diagnostics.run(
        common.DiagnoseRequest(target_id="mtls", stages=["grpc"], timeout_ms=2000)
    )


async def test_diagnostics_uses_production_mtls_credentials(
    gateway: GatewayHarness,
) -> None:
    response = await _diagnose_grpc(_diagnostics(gateway, _tls(gateway, "generator")))
    assert response.overall_status == "PASS", response.stages[0].message
    evidence = {item.name: item.value for item in response.stages[0].evidence}
    assert evidence == {"gw1.validated_variables": "1"}


@pytest.mark.parametrize("identity", [STRANGER, "generator"])
async def test_diagnostics_rejects_unauthorized_identity_and_wrong_ca(
    gateway: GatewayHarness, identity: str, tmp_path: Path
) -> None:
    ca_file = None
    if identity == "generator":
        ca_file = cast(Path, write_mtls_pki(tmp_path / "other-pki", ())["ca"])
    diagnostics = _diagnostics(gateway, _tls(gateway, identity, ca_file=ca_file))
    response = await _diagnose_grpc(diagnostics)
    assert response.overall_status == "FAIL"
    assert response.stages[0].error_code == "DIAGNOSTIC_FAILED"
    assert response.stages[0].evidence == []


async def test_diagnostics_cannot_bypass_mtls_with_plaintext(
    gateway: GatewayHarness,
) -> None:
    response = await _diagnose_grpc(_diagnostics(gateway, None))
    assert response.overall_status == "FAIL"
    assert response.stages[0].error_code == "DIAGNOSTIC_FAILED"


def test_writer_rejects_non_loopback_and_dns_alias_without_mtls() -> None:
    writer = GatewayWriter()
    for endpoint in ("192.0.2.10:50051", "localhost:50051"):
        with pytest.raises(GeneratorError, match="mTLS is required for non-loopback"):
            writer._rpc._stub(endpoint)
    assert writer._rpc._channels == {}
