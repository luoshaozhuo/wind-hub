"""Real loopback Gateway RPC integration with per-caller Bearer authorization."""

from __future__ import annotations

from datetime import UTC, datetime

import grpc
import pytest

from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as rpc
from data_generator.domain.model import (
    DataBatch,
    DataPoint,
    GeneratorError,
    OutputMapping,
    OutputTarget,
)
from data_generator.domain.scenario import Scenario
from data_generator.infrastructure.adapters.outbound.gateway import GatewayWriter
from tests.support.gateway import reserve_port

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

_TOKEN_ENV = "WIND_HUB_TEST_DG_GATEWAY_TOKEN"
_TOKEN = "test-" + "a" * 40
_TARGET = "D01.power"


class _Gateway(rpc.IndustrialGatewayServiceServicer):
    """Enforce actual gRPC metadata on read, lease and update operations."""

    def __init__(self) -> None:
        self.lease_calls: list[pb.ClaimTargetsRequest] = []
        self.update_calls: list[pb.UpdateVariablesRequest] = []

    @staticmethod
    async def _authorize(context: grpc.aio.ServicerContext) -> None:
        tokens = [
            value for key, value in context.invocation_metadata() if key.lower() == "authorization"
        ]
        if tokens != [f"Bearer {_TOKEN}"]:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Bearer token required")

    async def ListVariables(  # noqa: N802
        self, request: pb.ListVariablesRequest, context: grpc.aio.ServicerContext
    ) -> pb.ListVariablesResponse:
        await self._authorize(context)
        assert request.gateway_id == "gw1"
        return pb.ListVariablesResponse(
            variables=[pb.Variable(variable_id=_TARGET, data_type="uint16", writable=True)]
        )

    async def ClaimTargets(  # noqa: N802
        self, request: pb.ClaimTargetsRequest, context: grpc.aio.ServicerContext
    ) -> pb.ClaimTargetsResponse:
        await self._authorize(context)
        self.lease_calls.append(request)
        if request.action not in {"ACQUIRE", "RENEW", "RELEASE"}:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid action")
        return pb.ClaimTargetsResponse(accepted=True, lease_token="lease-test-123")

    async def UpdateVariables(  # noqa: N802
        self, request: pb.UpdateVariablesRequest, context: grpc.aio.ServicerContext
    ) -> pb.UpdateVariablesResponse:
        await self._authorize(context)
        if request.lease_token != "lease-test-123":
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "no lease")
        self.update_calls.append(request)
        return pb.UpdateVariablesResponse(
            results=[pb.VariableUpdateResult(variable_id=_TARGET, success=True)]
        )


def _scenario(port: int) -> Scenario:
    return Scenario(
        scenario_id="token-integration",
        version="1",
        description="authorized real gRPC Gateway",
        source={},
        devices=("D01",),
        points={("D01", "power"): {"data_type": "uint16"}},
        mappings=(OutputMapping("D01", "power", OutputTarget("gw1", _TARGET)),),
        gateways={"gw1": f"127.0.0.1:{port}"},
    )


async def test_authenticated_gateway_lease_and_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    gateway = _Gateway()
    server = grpc.aio.server()
    rpc.add_IndustrialGatewayServiceServicer_to_server(gateway, server)
    port = reserve_port()
    assert server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    writer = GatewayWriter(timeout=2, gateway_token_env=_TOKEN_ENV)
    scenario = _scenario(port)
    try:
        await writer.validate(scenario)
        await writer.claim(scenario, "generator-owner")
        now = datetime.now(UTC)
        batch = DataBatch("generator", 1, now, (DataPoint("D01", "power", 41, now),))
        results = await writer.send(scenario, batch)
        assert [item.state for item in results] == ["SUCCESS"]
        assert len(gateway.update_calls) == 1
        assert gateway.update_calls[0].values[0].variable_id == _TARGET
        assert await writer.release(scenario, "generator-owner")
    finally:
        assert await writer.close()
        await server.stop(1)


def test_missing_token_and_remote_endpoint_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_TOKEN_ENV, raising=False)
    with pytest.raises(GeneratorError, match="token"):
        GatewayWriter(gateway_token_env=_TOKEN_ENV)
    monkeypatch.setenv(_TOKEN_ENV, _TOKEN)
    writer = GatewayWriter(gateway_token_env=_TOKEN_ENV)
    with pytest.raises(GeneratorError, match="loopback"):
        writer._rpc._stub("192.168.1.20:50073")


async def test_gateway_rejects_wrong_token_even_on_loopback() -> None:
    gateway = _Gateway()
    server = grpc.aio.server()
    rpc.add_IndustrialGatewayServiceServicer_to_server(gateway, server)
    port = reserve_port()
    assert server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = rpc.IndustrialGatewayServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.ListVariables(
                    pb.ListVariablesRequest(gateway_id="gw1"),
                    metadata=(("authorization", "Bearer wrong"),),
                )
            assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await server.stop(1)
