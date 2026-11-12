"""Verified Data Generator outbound control transport for Gateway protocol-write events.

The adapter does not retry ambiguous submissions. A caller owns queue bounds,
per-target ordering, and the immutable write-event request ID.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import grpc

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts.scalar_codec import encode_scalar
from industrial_gateway.application.variable_store import Scalar
from industrial_gateway.infrastructure.config.v1_schema import TlsConfig, service_path


@dataclass(frozen=True, slots=True)
class GeneratorReceipt:
    accepted: bool
    effective_step: int
    message: str
    error_code: str


class GeneratorRpcClient:
    """Bind a protocol-write event to the currently active Generation, once."""

    def __init__(
        self,
        *,
        endpoint: str,
        tls: TlsConfig,
        project_root: Path,
        scenario_id: str,
    ) -> None:
        if not endpoint.strip() or not scenario_id.strip():
            raise ValueError("Generator endpoint and scenario binding are required")
        self._endpoint = endpoint
        self._tls = tls
        self._project_root = project_root
        self._scenario_id = scenario_id
        self._channel: grpc.aio.Channel | None = None
        self._stub: dg_rpc.DataGeneratorServiceAsyncStub | None = None

    def start(self) -> None:
        """Create the authenticated channel; never silently downgrade to plaintext."""
        if self._stub is not None:
            raise RuntimeError("Generator client already started")
        if not self._tls.enabled:
            raise ValueError("Generator input control requires mutual TLS")
        if (
            self._tls.ca_file is None
            or self._tls.cert_file is None
            or self._tls.key_file is None
        ):
            raise ValueError("Generator control requires complete client certificates")
        credentials = grpc.ssl_channel_credentials(
            root_certificates=service_path(
                self._project_root, self._tls.ca_file
            ).read_bytes(),
            private_key=service_path(
                self._project_root, self._tls.key_file
            ).read_bytes(),
            certificate_chain=service_path(
                self._project_root, self._tls.cert_file
            ).read_bytes(),
        )
        channel = grpc.aio.secure_channel(self._endpoint, credentials)
        self._channel = channel
        self._stub = dg_rpc.DataGeneratorServiceStub(channel)

    def _ready(self) -> dg_rpc.DataGeneratorServiceAsyncStub:
        if self._stub is None:
            raise RuntimeError("Generator client has not started")
        return self._stub

    async def submit(
        self,
        *,
        device_id: str,
        point_id: str,
        value: Scalar,
        request_id: str,
        timeout: float,
    ) -> GeneratorReceipt:
        """Resolve and verify the Generation before each single outbound submit."""
        if (
            not device_id.strip()
            or not point_id.strip()
            or not request_id.strip()
            or timeout <= 0
        ):
            raise ValueError("invalid Generator submission identity or deadline")
        stub = self._ready()
        deadline = asyncio.get_running_loop().time() + timeout

        def remaining() -> float:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise TimeoutError("Generator control deadline expired")
            return left

        device = await stub.GetDevice(
            common.ObjectId(id=device_id), timeout=remaining()
        )
        generation_id = device.device.generation_id
        if device.device.actual_state != "RUNNING" or not generation_id:
            raise ValueError("target device has no running Generation")
        generation = await stub.GetGeneration(
            common.ObjectId(id=generation_id), timeout=timeout
        )
        bound = generation.generation
        if (
            bound.generation_id != generation_id
            or bound.scenario_id != self._scenario_id
            or bound.actual_state != "RUNNING"
            or bound.desired_state != "RUNNING"
        ):
            raise ValueError("Generation binding changed or does not match scenario")

        # Discover from the server, not from a second Gateway-side input whitelist.
        token = ""
        seen_tokens: set[str] = set()
        writable = False
        for _ in range(100):
            listing = await stub.ListVariables(
                dg.ListVariablesRequest(
                    generation_id=generation_id,
                    device_id=device_id,
                    page_size=500,
                    page_token=token,
                ),
                timeout=remaining(),
            )
            if listing.generation_state != "RUNNING":
                raise ValueError("Generation is no longer running")
            for item in listing.variables:
                if item.point_id == point_id:
                    if not item.writable or item.role != "INPUT":
                        raise PermissionError("Generator model input is not writable")
                    writable = True
                    break
            if writable or not listing.next_page_token:
                break
            token = listing.next_page_token
            if token in seen_tokens:
                raise ValueError("Generator variable pagination repeated a token")
            seen_tokens.add(token)
        else:
            raise ValueError("Generator variable catalog pagination limit exceeded")
        if not writable:
            raise ValueError("control point is not an authorized Generator input")

        # The model service performs authoritative atomic validation and fencing.
        # Transport failure after submission remains UNKNOWN to the caller.
        response = await stub.UpdateVariables(
            dg.UpdateVariablesRequest(
                generation_id=generation_id,
                device_id=device_id,
                request_id=request_id,
                values=[
                    dg.VariableUpdate(point_id=point_id, value=encode_scalar(value))
                ],
            ),
            timeout=remaining(),
        )
        return GeneratorReceipt(
            accepted=response.accepted,
            effective_step=response.effective_step,
            message=response.message,
            error_code=response.error_code,
        )

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        self._stub = None
