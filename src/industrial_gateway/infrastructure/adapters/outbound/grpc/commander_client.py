"""Commander protobuf transport used only at the Gateway gRPC adapter boundary."""

from __future__ import annotations

import os
from datetime import datetime

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc as rpc
from contracts import common_pb2 as common
from contracts.scalar_codec import encode_scalar
from industrial_gateway.application.forwarding import CommandQuery, SubmissionReceipt
from industrial_gateway.application.variable_store import Scalar
from industrial_gateway.infrastructure.config.schema import ControlTarget


class CommanderRpcClient:
    """Hold Commander channel and translate protobufs into primitive results."""

    def __init__(self, config: ControlTarget) -> None:
        self._config = config
        if config.endpoint is None:
            raise ValueError("Commander endpoint required")
        self._endpoint = config.endpoint
        self._metadata: tuple[tuple[str, str], ...] = ()
        self._channel: grpc.aio.Channel | None = None
        self._stub: rpc.CommanderServiceAsyncStub | None = None

    def start(self) -> None:
        """Use an authenticated loopback channel; never fall back to anonymous RPC."""
        if self._stub is not None:
            raise RuntimeError("Commander channel already started")
        host, sep, port = self._endpoint.rpartition(":")
        if not sep or host not in {"127.0.0.1", "::1", "localhost"} or not port.isdecimal():
            raise ValueError("Commander endpoint must be loopback host:port")
        if not 1 <= int(port) <= 65535:
            raise ValueError("Commander endpoint port out of range")
        reference = self._config.token_env
        token = os.environ.get(reference or "", "")
        if len(token) < 32 or not token.isascii() or any(not 33 <= ord(ch) <= 126 for ch in token):
            raise ValueError("Commander outbound token is missing or invalid")
        self._channel = grpc.aio.insecure_channel(self._endpoint)
        self._metadata = (("authorization", f"Bearer {token}"),)
        self._stub = rpc.CommanderServiceStub(self._channel)

    def _ready(self) -> rpc.CommanderServiceAsyncStub:
        if self._stub is None:
            raise RuntimeError("Commander channel not started")
        return self._stub

    async def get_revision(self, device_id: str, timeout: float) -> str:
        response = await self._ready().GetDevice(
            common.ObjectId(id=device_id), timeout=timeout, metadata=self._metadata
        )
        return response.device.revision

    async def submit(
        self,
        *,
        device_id: str,
        point_id: str,
        value: Scalar,
        expires_at: datetime,
        expected_revision: str,
        request_id: str,
        timeout: float,
    ) -> SubmissionReceipt:
        stamp = Timestamp()
        stamp.FromDatetime(expires_at)
        response = await self._ready().SubmitCommand(
            pb.SubmitCommandRequest(
                device_id=device_id,
                point_id=point_id,
                value=encode_scalar(value),
                expires_at=stamp,
                expected_revision=expected_revision,
                request_id=request_id,
            ),
            timeout=timeout,
            metadata=self._metadata,
        )
        return SubmissionReceipt(
            accepted=response.accepted,
            command_id=response.command_id,
            message=response.message,
        )

    async def get_command_result(self, command_id: str, timeout: float) -> CommandQuery:
        response = await self._ready().GetCommandResult(
            pb.GetCommandResultRequest(command_id=command_id),
            timeout=timeout,
            metadata=self._metadata,
        )
        return CommandQuery(state=response.state)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        self._stub = None
        self._metadata = ()
