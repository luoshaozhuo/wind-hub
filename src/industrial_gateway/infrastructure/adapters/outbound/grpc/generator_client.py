"""Authenticated Data Generator input adapter for Commander-authorized simulation.

The adapter does not retry ambiguous submissions. The application layer owns
request identity, ordering, and transport uncertainty.
"""

from __future__ import annotations

import asyncio

import grpc

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts.scalar_codec import encode_scalar
from industrial_gateway.application.ports.simulation import SimulationInputReceipt
from industrial_gateway.application.variable_store import Scalar
from core.infrastructure.grpc_bearer import bearer_metadata


class GeneratorRpcClient:
    """Bind an authorized simulation request to the active Generation."""

    def __init__(
        self,
        *,
        endpoint: str,
        token_env: str,
        scenario_id: str,
    ) -> None:
        if not endpoint.strip() or not scenario_id.strip():
            raise ValueError("Generator endpoint and scenario binding are required")
        self._endpoint = endpoint
        self._token_env = token_env
        self._scenario_id = scenario_id
        self._channel: grpc.aio.Channel | None = None
        self._stub: dg_rpc.DataGeneratorServiceAsyncStub | None = None

    def start(self) -> None:
        """Create the authenticated channel; never silently downgrade to plaintext."""
        if self._stub is not None:
            raise RuntimeError("Generator client already started")
        bearer_metadata(self._token_env)
        channel = grpc.aio.insecure_channel(self._endpoint)
        self._channel = channel
        self._stub = dg_rpc.DataGeneratorServiceStub(channel)

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        return bearer_metadata(self._token_env)

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
    ) -> SimulationInputReceipt:
        """Resolve and verify the Generation before each single outbound submit."""
        if not device_id.strip() or not point_id.strip() or not request_id.strip() or timeout <= 0:
            raise ValueError("invalid Generator submission identity or deadline")
        stub = self._ready()
        deadline = asyncio.get_running_loop().time() + timeout

        def remaining() -> float:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise TimeoutError("Generator control deadline expired")
            return left

        device = await stub.GetDevice(common.ObjectId(id=device_id), timeout=remaining(), metadata=self._metadata())
        generation_id = device.device.generation_id
        if device.device.actual_state != "RUNNING" or not generation_id:
            raise ValueError("target device has no running Generation")
        generation = await stub.GetGeneration(
            common.ObjectId(id=generation_id), timeout=remaining()
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
            metadata=self._metadata(),
                metadata=self._metadata(),
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
                values=[dg.VariableUpdate(point_id=point_id, value=encode_scalar(value))],
            ),
            timeout=remaining(),
        )
        return SimulationInputReceipt(
            accepted=response.accepted,
            effective_step=response.effective_step,
            generation_id=generation_id,
            message=response.message,
            error_code=response.error_code,
        )

    async def get_execution(self, *, generation_id: str, request_id: str, timeout: float) -> str:
        """Return only Generator-signed per-request completion evidence."""
        response = await self._ready().GetInputExecution(
            dg.GetInputExecutionRequest(
                generation_id=generation_id,
                request_id=request_id,
            ),
            timeout=timeout,
            metadata=self._metadata(),
        )
        if response.request_id != request_id:
            return "UNKNOWN"
        return response.state if response.state in {"PENDING", "APPLIED"} else "UNKNOWN"

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        self._stub = None
