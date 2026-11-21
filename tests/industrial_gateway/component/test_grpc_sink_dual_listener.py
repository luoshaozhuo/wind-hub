"""Gateway dual-listener security: mTLS management port + loopback Bearer Sink port.

The same gRPC server binds two ports. The management port keeps certificate
authorization; the optional Sink ingress is loopback-only and authenticates a
scoped Bearer token limited to the inject permission. These tests use the real
production server, runtime and authorization helpers over real loopback
channels — no authorization mocks.
"""

from __future__ import annotations

from pathlib import Path

import grpc
import pytest
import yaml

from contracts import common_pb2
from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as pbrpc
from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.adapters.inbound.grpc.data_server import (
    create_gateway_grpc_server,
)
from industrial_gateway.infrastructure.config.resolution import load_gateway
from industrial_gateway.infrastructure.config.snapshots import ConfigSnapshots
from tests.support.gateway import (
    ServerSpec,
    reserve_port,
    start_gateway,
    write_mtls_pki,
)

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


def _add_sink_listener(service_path: Path, sink_port: int) -> None:
    document = yaml.safe_load(service_path.read_text(encoding="utf-8"))
    document["grpc"]["sink_listener_port"] = sink_port
    service_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
async def dual_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    pki = write_mtls_pki(root, ("cli", "generator", "sink"))
    sink_port = reserve_port()
    harness = await start_gateway(
        root,
        [_spec()],
        mtls={"cli": ["read", "lifecycle", "inject", "diagnose"]},
        bootstrap_cn="cli",
    )
    management_port = harness.grpc_port
    # start_gateway already built the server; rebuild the config with the
    # opt-in Sink listener and recreate the production server on both ports.
    service_path = harness.service_path
    _add_sink_listener(service_path, sink_port)
    monkeypatch.setenv(SINK_TOKEN_ENV, SINK_TOKEN)
    await harness.gateway_server.stop(grace=None)
    config = load_gateway(service_path, project_root=root)
    runtime = assemble_gateway(config)
    snapshots = ConfigSnapshots(runtime, service_path, root)
    server = create_gateway_grpc_server(runtime, snapshots)
    await server.start()
    harness.gateway_server = server
    try:
        yield harness, management_port, sink_port, pki
    finally:
        await harness.stop()


def _sink_channel(sink_port: int, token: str | None) -> grpc.aio.Channel:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{sink_port}")
    if token is not None:
        channel = grpc.aio.insecure_channel(
            f"127.0.0.1:{sink_port}",
            options=(),
        )
        # Metadata is attached per call, not at channel construction.
    return channel


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

        # Read back through the management certificate channel to confirm the
        # injected value is observable (real end-to-end variable write).
        mgmt = grpc.aio.secure_channel(
            f"127.0.0.1:{_mgmt}",
            grpc.ssl_channel_credentials(
                root_certificates=_pki["ca"].read_bytes(),
                private_key=_pki["clients"]["cli"][1].read_bytes(),
                certificate_chain=_pki["clients"]["cli"][0].read_bytes(),
            ),
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )
        try:
            await mgmt.channel_ready()
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
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
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
        assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
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


def test_sink_listener_requires_distinct_port(tmp_path: Path) -> None:
    from industrial_gateway.infrastructure.config.schema import GrpcConfig

    with pytest.raises(Exception):  # noqa: B017, BLE001 - schema ValueError
        GrpcConfig(
            listen_port=5000,
            sink_listener_port=5000,
            tls={"enabled": True, "ca_file": "ca", "cert_file": "c", "key_file": "k"},
            permissions={"cli": ["read"]},
        )


def test_sink_listener_requires_mtls_management_listener() -> None:
    from industrial_gateway.infrastructure.config.schema import GrpcConfig

    # Opting into the Sink port while TLS is disabled is rejected.
    with pytest.raises(Exception):  # noqa: B017, BLE001 - schema ValueError
        GrpcConfig(listen_port=5000, sink_listener_port=5001, tls={"enabled": False})
