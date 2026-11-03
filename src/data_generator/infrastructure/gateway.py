"""Bounded Gateway UpdateVariables fan-out with explicit unknown-result isolation."""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque
from collections.abc import Sequence

import grpc

from contracts import common_pb2, industrial_gateway_pb2, industrial_gateway_pb2_grpc
from core.infrastructure.rpc.scalar_codec import encode_scalar
from data_generator.application.config import Scenario
from data_generator.domain.model import DataBatch, DeliveryResult, OutputTarget, Scalar

_CLEAR_REJECTION_CODES = frozenset({
    grpc.StatusCode.INVALID_ARGUMENT,
    grpc.StatusCode.NOT_FOUND,
    grpc.StatusCode.PERMISSION_DENIED,
    grpc.StatusCode.UNAUTHENTICATED,
    grpc.StatusCode.FAILED_PRECONDITION,
})


class GatewayWriter:
    """Each Gateway is serialized; the global RPC permit follows real call lifetime."""

    def __init__(
        self, timeout: float = 3.0, max_points: int = 1000, max_inflight: int = 1,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Gateway timeout must be positive and finite")
        if max_points <= 0 or max_inflight <= 0:
            raise ValueError("Gateway resource bounds must be positive")
        self._timeout = timeout
        self._max_points = max_points
        self._available = asyncio.Semaphore(max_inflight)
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: set[asyncio.Future[object]] = set()
        self._unsettled: dict[str, asyncio.Future[object]] = {}
        self._unsafe: set[str] = set()
        self.rpc_durations: deque[tuple[float, str, float]] = deque(maxlen=4096)

    @property
    def unsafe_gateways(self) -> frozenset[str]:
        """Gateway IDs with unresolved side effects requiring explicit isolation."""
        return frozenset(self._unsafe)

    async def send(self, scenario: Scenario, batch: DataBatch) -> list[DeliveryResult]:
        values = {(point.device_id, point.point_id): point.value for point in batch.points}
        groups: dict[str, list[tuple[OutputTarget, Scalar]]] = defaultdict(list)
        for mapping in scenario.mappings:
            key = (mapping.device_id, mapping.point_id)
            if key in values:
                groups[mapping.target.gateway_id].append((mapping.target, values[key]))

        async def group(gateway_id: str, entries: list[tuple[OutputTarget, Scalar]]
                        ) -> list[DeliveryResult]:
            outcomes: list[DeliveryResult] = []
            for offset in range(0, len(entries), self._max_points):
                outcomes.extend(await self._send_one(
                    scenario, gateway_id, entries[offset:offset + self._max_points],
                ))
            return outcomes

        individual = await asyncio.gather(
            *(group(gateway_id, entries) for gateway_id, entries in groups.items())
        )
        return [outcome for group_results in individual for outcome in group_results]

    async def _send_one(
        self,
        scenario: Scenario,
        gateway_id: str,
        entries: Sequence[tuple[OutputTarget, Scalar]],
    ) -> list[DeliveryResult]:
        async with self._locks[gateway_id]:
            if gateway_id in self._unsafe:
                return [DeliveryResult(target, "UNKNOWN", "UNSETTLED_OUTPUT")
                        for target, _ in entries]
            prior = self._unsettled.get(gateway_id)
            if prior is not None and not prior.done():
                return [DeliveryResult(target, "UNKNOWN", "UNSETTLED_IO")
                        for target, _ in entries]
            self._unsettled.pop(gateway_id, None)
            try:
                await asyncio.wait_for(self._available.acquire(), timeout=self._timeout)
            except TimeoutError:
                return [DeliveryResult(target, "FAILED", "BACKPRESSURE_TIMEOUT")
                        for target, _ in entries]
            try:
                endpoint = scenario.gateways[gateway_id]
                if endpoint not in self._channels:
                    self._channels[endpoint] = grpc.aio.insecure_channel(endpoint)
                stub = industrial_gateway_pb2_grpc.IndustrialGatewayServiceStub(
                    self._channels[endpoint]
                )
                request = industrial_gateway_pb2.UpdateVariablesRequest(
                    gateway_id=gateway_id,
                    values=[
                        industrial_gateway_pb2.VariableUpdate(
                            variable_id=target.variable_id, value=encode_scalar(value),
                        )
                        for target, value in entries
                    ],
                )
                started = time.monotonic()
                task = asyncio.ensure_future(
                    stub.UpdateVariables(request, timeout=self._timeout * 2)
                )
            except BaseException:
                self._available.release()
                raise
            self._unsettled[gateway_id] = task
            self._pending.add(task)
            expected = {target.variable_id for target, _ in entries}

            def on_complete(completed: asyncio.Future[object]) -> None:
                self._pending.discard(completed)
                self._available.release()
                self.rpc_durations.append(
                    (time.monotonic(), gateway_id, (time.monotonic() - started) * 1000)
                )
                try:
                    reply = completed.result()
                except grpc.RpcError as error:
                    if error.code() in _CLEAR_REJECTION_CODES:
                        self._unsafe.discard(gateway_id)
                except BaseException:
                    pass
                else:
                    returned = {item.variable_id for item in reply.results}
                    if returned == expected:
                        self._unsafe.discard(gateway_id)

            task.add_done_callback(on_complete)
            try:
                response = await asyncio.wait_for(asyncio.shield(task), self._timeout)
            except grpc.RpcError as error:
                code = error.code()
                if code in _CLEAR_REJECTION_CODES:
                    return [DeliveryResult(target, "FAILED", str(code), str(error))
                            for target, _ in entries]
                self._unsafe.add(gateway_id)
                return [DeliveryResult(target, "UNKNOWN", str(code))
                        for target, _ in entries]
            except TimeoutError:
                self._unsafe.add(gateway_id)
                return [DeliveryResult(target, "UNKNOWN", "RPC_TIMEOUT")
                        for target, _ in entries]
            except asyncio.CancelledError:
                self._unsafe.add(gateway_id)
                raise

            received = {item.variable_id: item for item in response.results}
            results: list[DeliveryResult] = []
            for target, _ in entries:
                item = received.get(target.variable_id)
                if item is None:
                    self._unsafe.add(gateway_id)
                    results.append(DeliveryResult(target, "UNKNOWN", "MISSING_ACK"))
                elif item.success:
                    results.append(DeliveryResult(target, "SUCCESS"))
                else:
                    results.append(DeliveryResult(
                        target, "FAILED", item.error_code, item.message,
                    ))
            return results

    async def drain(self) -> None:
        """Await the native call objects; unsafe results remain isolated."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()
        for channel in self._channels.values():
            await channel.close()
        self._channels.clear()
