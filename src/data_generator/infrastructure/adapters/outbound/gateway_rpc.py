"""gRPC transport adapter for Data Generator Gateway variable output."""

from __future__ import annotations

import grpc

from contracts import industrial_gateway_pb2, industrial_gateway_pb2_grpc
from contracts.scalar_codec import encode_scalar
from data_generator.application.ports.gateway_rpc import (
    GatewayCallError,
    GatewayLease,
    GatewayVariable,
    GatewayVariableResult,
    VariablePage,
)
from data_generator.domain.model import Scalar
from data_generator.infrastructure.gateway_tls import (
    create_gateway_channel,
    gateway_credentials,
)


class GatewayRpcClient:
    """Own exactly one authenticated Channel per explicitly configured endpoint."""

    def __init__(self, tls: dict[str, str] | None) -> None:
        self._credentials = gateway_credentials(tls)
        self._channels: dict[str, grpc.aio.Channel] = {}

    def _stub(self, endpoint: str) -> industrial_gateway_pb2_grpc.IndustrialGatewayServiceAsyncStub:
        channel = self._channels.get(endpoint)
        if channel is None:
            channel = create_gateway_channel(endpoint, self._credentials)
            self._channels[endpoint] = channel
        return industrial_gateway_pb2_grpc.IndustrialGatewayServiceStub(channel)

    @staticmethod
    def _error(exc: grpc.RpcError) -> GatewayCallError:
        code = exc.code()
        return GatewayCallError(code.name if isinstance(code, grpc.StatusCode) else str(code))

    async def list_variables(
        self, endpoint: str, gateway_id: str, page_token: str, timeout: float
    ) -> VariablePage:
        try:
            result = await self._stub(endpoint).ListVariables(
                industrial_gateway_pb2.ListVariablesRequest(
                    gateway_id=gateway_id,
                    page_token=page_token,
                    page_size=1000,
                ),
                timeout=timeout,
            )
        except grpc.RpcError as exc:
            raise self._error(exc) from exc
        return VariablePage(
            tuple(
                GatewayVariable(item.variable_id, item.data_type, item.writable)
                for item in result.variables
            ),
            result.next_page_token,
        )

    async def claim(
        self,
        endpoint: str,
        gateway_id: str,
        owner_id: str,
        names: tuple[str, ...],
        action: str,
        token: str,
        lease_ms: int,
        timeout: float,
    ) -> GatewayLease:
        try:
            reply = await self._stub(endpoint).ClaimTargets(
                industrial_gateway_pb2.ClaimTargetsRequest(
                    gateway_id=gateway_id,
                    owner_id=owner_id,
                    variable_ids=names,
                    action=action,
                    lease_token=token,
                    lease_ms=lease_ms,
                ),
                timeout=timeout,
            )
        except grpc.RpcError as exc:
            raise self._error(exc) from exc
        return GatewayLease(reply.accepted, reply.lease_token, reply.error_code)

    async def update(
        self,
        endpoint: str,
        gateway_id: str,
        token: str,
        values: tuple[tuple[str, Scalar], ...],
        timeout: float,
    ) -> tuple[GatewayVariableResult, ...]:
        try:
            result = await self._stub(endpoint).UpdateVariables(
                industrial_gateway_pb2.UpdateVariablesRequest(
                    gateway_id=gateway_id,
                    lease_token=token,
                    values=[
                        industrial_gateway_pb2.VariableUpdate(
                            variable_id=variable_id,
                            value=encode_scalar(value),
                        )
                        for variable_id, value in values
                    ],
                ),
                timeout=timeout,
            )
        except grpc.RpcError as exc:
            raise self._error(exc) from exc
        return tuple(
            GatewayVariableResult(item.variable_id, item.success, item.error_code, item.message)
            for item in result.results
        )

    async def close(self) -> None:
        for channel in self._channels.values():
            await channel.close()
        self._channels.clear()
