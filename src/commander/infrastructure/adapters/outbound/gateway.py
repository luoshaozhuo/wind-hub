"""Commander outbound simulation adapter using the official Gateway execution RPCs."""

from __future__ import annotations

import asyncio
import os
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

_CONFIRMED_GATEWAY_REJECTIONS = frozenset(
    {
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.ALREADY_EXISTS,
        grpc.StatusCode.NOT_FOUND,
    }
)


def _rpc_error_result(point_id: str, exc: grpc.aio.AioRpcError) -> ProtocolWriteResult:
    outcome = (
        WriteOutcome.CONFIRMED_FAILURE
        if exc.code() in _CONFIRMED_GATEWAY_REJECTIONS
        else WriteOutcome.UNKNOWN
    )
    return ProtocolWriteResult(point_id, outcome, exc.code().name)


class GatewaySimulationSession:
    """A command-aware write session; never exposes raw protocol writing."""

    def __init__(self, device_id: str, target: SimulationGatewayTarget) -> None:
        self._device_id = device_id
        self._target = target
        self._channel: grpc.aio.Channel | None = None
        self._client: gateway_rpc.IndustrialGatewayServiceAsyncStub | None = None
        self._metadata: tuple[tuple[str, str], ...] = ()

    async def connect(self) -> None:
        if self._client is not None:
            return
        host, separator, port = self._target.endpoint.rpartition(":")
        if (
            not separator
            or host not in {"127.0.0.1", "localhost", "::1"}
            or not port.isdecimal()
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError("simulation Gateway must use a loopback endpoint")
        token = os.environ.get(self._target.token_env, "")
        if len(token) < 32 or not token.isascii() or any(not 33 <= ord(ch) <= 126 for ch in token):
            raise ValueError("simulation Gateway token is missing or invalid")
        channel = grpc.aio.insecure_channel(self._target.endpoint)
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=3.0)
        except BaseException:
            await channel.close()
            raise
        self._channel = channel
        self._metadata = (("authorization", f"Bearer {token}"),)
        self._client = gateway_rpc.IndustrialGatewayServiceStub(channel)

    def is_open(self) -> bool:
        return self._client is not None

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset({ProtocolCapability.WRITE})

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        raise RuntimeError("simulation writes require a durable command identity")

    async def write_many(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
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
        try:
            gateway_info = await client.GetGateway(
                common_pb2.ObjectId(id=self._target.gateway_id),
                timeout=3.0,
                metadata=self._metadata,
            )
        except grpc.aio.AioRpcError as exc:
            return _rpc_error_result(spec.point_id, exc)
        revision = gateway_info.gateway.revision
        if not revision:
            return ProtocolWriteResult(spec.point_id, WriteOutcome.UNKNOWN, "GATEWAY_NO_REVISION")
        if expiry <= datetime.now(UTC):
            return ProtocolWriteResult(spec.point_id, WriteOutcome.CONFIRMED_FAILURE, "EXPIRED")
        timestamp = Timestamp()
        timestamp.FromDatetime(expiry)
        try:
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
                metadata=self._metadata,
            )
        except grpc.aio.AioRpcError as exc:
            # Deterministic Gateway rejections are failures; I/O stays UNKNOWN.
            return _rpc_error_result(spec.point_id, exc)
        if not response.accepted and response.state == "REJECTED":
            return ProtocolWriteResult(
                spec.point_id, WriteOutcome.CONFIRMED_FAILURE, response.error_code
            )
        if not response.accepted or response.state == "UNKNOWN":
            return ProtocolWriteResult(
                spec.point_id, WriteOutcome.UNKNOWN, response.error_code or response.state
            )
        # Acceptance reserves a future model step; poll only for execution proof.
        # Query failure is ambiguous. Never resubmit with a new request identity.
        while response.state in {"ACCEPTED", "PENDING"}:
            remaining = (expiry - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return ProtocolWriteResult(
                    spec.point_id, WriteOutcome.UNKNOWN, "EXECUTION_CONFIRMATION_TIMEOUT"
                )
            await asyncio.sleep(min(0.05, remaining))
            remaining = (expiry - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                break
            try:
                response = await client.GetSimulationExecutionResult(
                    gateway.SimulationExecutionQuery(
                        gateway_id=self._target.gateway_id,
                        request_id=command.command_id,
                    ),
                    timeout=remaining,
                    metadata=self._metadata,
                )
            except grpc.aio.AioRpcError as exc:
                return _rpc_error_result(spec.point_id, exc)
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
        self._metadata = ()
        if channel is not None:
            await channel.close()
