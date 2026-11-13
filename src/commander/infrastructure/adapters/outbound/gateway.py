"""Commander outbound simulation adapter using the official Gateway execution RPCs."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from commander.application.settings import SimulationGatewayTarget
from commander.domain.command import AcceptedCommand
from contracts import common_pb2
from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from contracts.scalar_codec import encode_scalar
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)


class GatewaySimulationSession:
    """A command-aware write session; never exposes raw protocol writing."""

    def __init__(self, device_id: str, target: SimulationGatewayTarget) -> None:
        self._device_id = device_id
        self._target = target
        self._channel: grpc.aio.Channel | None = None
        self._client: gateway_rpc.IndustrialGatewayServiceAsyncStub | None = None

    async def connect(self) -> None:
        if self._client is not None:
            return
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self._target.ca_cert.read_bytes(),
            private_key=self._target.client_key.read_bytes(),
            certificate_chain=self._target.client_cert.read_bytes(),
        )
        channel = grpc.aio.secure_channel(self._target.endpoint, credentials)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=3.0)
        except BaseException:
            await channel.close()
            raise
        self._channel = channel
        self._client = gateway_rpc.IndustrialGatewayServiceStub(channel)

    def is_open(self) -> bool:
        return self._client is not None

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.WRITE})

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        raise RuntimeError("simulation writes require a durable command identity")

    async def write_many(
        self, writes: Sequence[ProtocolWrite]
    ) -> tuple[ProtocolWriteResult, ...]:
        raise RuntimeError("simulation writes require per-command identities")

    async def write_authorized(self, command: AcceptedCommand) -> ProtocolWriteResult:
        """Return success only with confirmed APPLIED evidence, never on acceptance."""
        client = self._client
        if client is None or command.spec.device_id != self._device_id:
            raise RuntimeError("simulation session is not connected to the command device")
        spec = command.spec
        expiry = spec.expires_at or datetime.now(UTC) + timedelta(seconds=3)
        if expiry <= datetime.now(UTC):
            return ProtocolWriteResult(spec.point_id, WriteOutcome.CONFIRMED_FAILURE, "EXPIRED")
        # Gateway is the authority for its running configuration revision.
        gateway_info = await client.GetGateway(
            common_pb2.ObjectId(id=self._target.gateway_id),
            timeout=3.0,
        )
        revision = gateway_info.gateway.revision
        if not revision:
            return ProtocolWriteResult(spec.point_id, WriteOutcome.UNKNOWN, "GATEWAY_NO_REVISION")
        timestamp = Timestamp()
        timestamp.FromDatetime(expiry)
        response = await client.ExecuteSimulationCommand(
            gateway.SimulationCommandRequest(
                gateway_id=self._target.gateway_id,
                device_id=spec.device_id,
                point_id=spec.point_id,
                value=encode_scalar(spec.value),
                request_id=command.command_id,
                expected_revision=revision,
                expires_at=timestamp,
            ),
            timeout=max(0.001, (expiry - datetime.now(UTC)).total_seconds()),
        )
        if response.accepted and response.state == "APPLIED":
            return ProtocolWriteResult(spec.point_id, WriteOutcome.CONFIRMED_SUCCESS)
        if not response.accepted and response.state == "REJECTED":
            return ProtocolWriteResult(
                spec.point_id, WriteOutcome.CONFIRMED_FAILURE, response.error_code
            )
        return ProtocolWriteResult(
            spec.point_id, WriteOutcome.UNKNOWN, response.error_code or response.state
        )

    async def close(self) -> None:
        channel, self._channel = self._channel, None
        self._client = None
        if channel is not None:
            await channel.close()
