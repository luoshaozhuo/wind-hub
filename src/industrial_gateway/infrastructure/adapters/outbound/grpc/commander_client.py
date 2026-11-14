"""Commander protobuf transport used only at the Gateway gRPC adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import commander_pb2 as pb
from contracts import commander_pb2_grpc as rpc
from contracts import common_pb2 as common
from contracts.scalar_codec import encode_scalar
from industrial_gateway.application.variable_store import Scalar
from industrial_gateway.infrastructure.config.schema import (
    ControlTarget,
    TlsConfig,
    service_path,
)


@dataclass(frozen=True, slots=True)
class SubmissionReceipt:
    accepted: bool
    command_id: str
    message: str


@dataclass(frozen=True, slots=True)
class CommandQuery:
    state: str


class CommanderRpcClient:
    """Hold Commander channel and translate protobufs into primitive results."""

    def __init__(self, config: ControlTarget, project_root: Path) -> None:
        self._config = config
        if config.endpoint is None:
            raise ValueError("Commander endpoint required")
        self._endpoint = config.endpoint
        self._project_root = project_root
        self._channel: grpc.aio.Channel | None = None
        self._stub: rpc.CommanderServiceAsyncStub | None = None

    def start(self) -> None:
        if self._stub is not None:
            raise RuntimeError("Commander channel already started")
        tls = self._config.tls or TlsConfig()
        if tls.enabled:
            if tls.ca_file is None or tls.cert_file is None or tls.key_file is None:
                raise ValueError("Commander mTLS requires CA, certificate and key")
            credentials = grpc.ssl_channel_credentials(
                root_certificates=service_path(self._project_root, tls.ca_file).read_bytes(),
                private_key=service_path(self._project_root, tls.key_file).read_bytes(),
                certificate_chain=service_path(self._project_root, tls.cert_file).read_bytes(),
            )
            channel = grpc.aio.secure_channel(self._endpoint, credentials)
        else:
            channel = grpc.aio.insecure_channel(self._endpoint)
        self._channel = channel
        self._stub = rpc.CommanderServiceStub(channel)

    def _ready(self) -> rpc.CommanderServiceAsyncStub:
        if self._stub is None:
            raise RuntimeError("Commander channel not started")
        return self._stub

    async def get_revision(self, device_id: str, timeout: float) -> str:
        response = await self._ready().GetDevice(common.ObjectId(id=device_id), timeout=timeout)
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
        )
        return CommandQuery(state=response.state)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        self._stub = None
