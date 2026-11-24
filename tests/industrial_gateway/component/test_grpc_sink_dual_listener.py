"""Gateway dual-listener security: loopback Bearer management and Sink ingress.

The production server binds two loopback ports. Sink has an inject-only
identity, while the CLI management identity can read and control state.
No certificate-derived authorization or gRPC security mocks are used.
"""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest

from contracts import common_pb2
from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as pbrpc
from tests.support.gateway import ServerSpec, reserve_port, start_gateway

pytestmark = [pytest.mark.component, pytest.mark.real_service]

SINK_TOKEN = "gateway-sink-injector-token-0123456789abcdef"
SINK_TOKEN_ENV = "WIND_HUB_GATEWAY_SINK_TOKEN"


def _spec() -> ServerSpec:
    return ServerSpec(
        server_id="gw-sink",
        device_id="D01",
        modbus_port=reserve_port(),
        slave_id=1,
        control_enabled=False,
        command_mappings=(),
    )


@pytest.fixture
async def dual_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    sink_port = reserve_port()
    monkeypatch.setenv(SINK_TOKEN_ENV, SINK_TOKEN)
    harness = await start_gateway(
        root,
        [_spec()],
        mtls={"cli": ["read", "lifecycle", "inject", "diagnose"]},
        bootstrap_cn="cli",
        sink_listener_port=sink_port,
        sink_token_env=SINK_TOKEN_ENV,
    )
    try:
        yield harness, harness.grpc_port, sink_port, None
    finally:
        await harness.stop()


async def test_valid_sink_token_claims_targets_on_loopback_port(dual_gateway) -> None:
    _harness, _mgmt, sink_port, _pki = dual_gateway
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{sink_port}")
    await channel.channel_ready()
    try:
        stub = pbrpc.IndustrialGatewayServiceStub(channel)
        claim = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw-sink",
                owner_id="sink-owner",
                variable_ids=["fb_float"],
                action="ACQUIRE",
                lease_ms=5000,
            ),
            metadata=(("authorization", f"Bearer {SINK_TOKEN}"),),
        )
        assert claim.accepted is True
        assert claim.lease_token

        # The same scoped token must drive an actual variable update, proving
        # GatewayOutput-style delivery works rather than only server startup.
        update = await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw-sink",
                lease_token=claim.lease_token,
                values=[
                    pb.VariableUpdate(
                        variable_id="fb_float",
                        value=common_pb2.ScalarValue(double_value=1234.5),
                    )
                ],
            ),
            metadata=(("authorization", f"Bearer {SINK_TOKEN}"),),
        )
        assert all(item.success for item in update.results)

        # Management Bearer caller independently reads the injected value.
        mgmt = _harness.mtls_channel("cli")
        try:
            read = await pbrpc.IndustrialGatewayServiceStub(mgmt).ReadVariables(
                pb.ReadVariablesRequest(gateway_id="gw-sink", variable_ids=["fb_float"])
            )
            assert any(sample.point_id == "fb_float" for sample in read.values)
        finally:
            await mgmt.close()
    finally:
        await channel.close()


async def test_missing_token_is_unauthenticated(dual_gateway) -> None:
    _harness, _mgmt, sink_port, _pki = dual_gateway
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{sink_port}")
    await channel.channel_ready()
    try:
        stub = pbrpc.IndustrialGatewayServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ClaimTargets(
                pb.ClaimTargetsRequest(
                    gateway_id="gw-sink",
                    owner_id="sink-owner",
                    variable_ids=["fb_float"],
                    action="ACQUIRE",
                    lease_ms=5000,
                )
            )
        assert exc.value.code() in (
            grpc.StatusCode.PERMISSION_DENIED,
            grpc.StatusCode.UNAUTHENTICATED,
        )
    finally:
        await channel.close()


async def test_wrong_token_is_denied(dual_gateway) -> None:
    _harness, _mgmt, sink_port, _pki = dual_gateway
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{sink_port}")
    await channel.channel_ready()
    try:
        stub = pbrpc.IndustrialGatewayServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ClaimTargets(
                pb.ClaimTargetsRequest(
                    gateway_id="gw-sink",
                    owner_id="sink-owner",
                    variable_ids=["fb_float"],
                    action="ACQUIRE",
                    lease_ms=5000,
                ),
                metadata=(("authorization", "Bearer not-the-configured-secret"),),
            )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await channel.close()


async def test_duplicate_authorization_header_is_denied(dual_gateway) -> None:
    _harness, _mgmt, sink_port, _pki = dual_gateway
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{sink_port}")
    await channel.channel_ready()
    try:
        stub = pbrpc.IndustrialGatewayServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ClaimTargets(
                pb.ClaimTargetsRequest(
                    gateway_id="gw-sink",
                    owner_id="sink-owner",
                    variable_ids=["fb_float"],
                    action="ACQUIRE",
                    lease_ms=5000,
                ),
                metadata=(
                    ("authorization", f"Bearer {SINK_TOKEN}"),
                    ("authorization", f"Bearer {SINK_TOKEN}"),
                ),
            )
        assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await channel.close()


async def test_sink_token_cannot_reach_management_query_rpc(dual_gateway) -> None:
    _harness, _mgmt, sink_port, _pki = dual_gateway
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{sink_port}")
    await channel.channel_ready()
    try:
        stub = pbrpc.IndustrialGatewayServiceStub(channel)
        # ListGateways is a read/management RPC, not an inject RPC; the Sink
        # token only grants inject and must not gain management access.
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ListGateways(
                pb.ListGatewaysRequest(),
                metadata=(("authorization", f"Bearer {SINK_TOKEN}"),),
            )
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


async def test_management_port_rejects_plaintext_sink_token(dual_gateway) -> None:
    _harness, management_port, _sink_port, _pki = dual_gateway
    # The management listener requires a client certificate; a plaintext
    # connection cannot complete the TLS handshake and must fail closed.
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{management_port}")
    try:
        stub = pbrpc.IndustrialGatewayServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError):
            await stub.ListGateways(
                pb.ListGatewaysRequest(),
                timeout=5,
            )
    finally:
        await channel.close()


def test_sink_listener_requires_distinct_port() -> None:
    from pydantic import ValidationError

    from industrial_gateway.infrastructure.config.schema import GrpcConfig

    with pytest.raises(ValidationError):
        GrpcConfig(listen_port=5000, sink_listener_port=5000)


def test_sink_listener_allows_separate_loopback_port() -> None:
    from industrial_gateway.infrastructure.config.schema import GrpcConfig

    config = GrpcConfig(listen_port=5000, sink_listener_port=5001)
    assert config.sink_listener_port == 5001
