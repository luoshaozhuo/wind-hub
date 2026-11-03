"""Bounded Gateway UpdateVariables fan-out with explicit unknown-result isolation."""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from typing import Any

import grpc

from contracts import industrial_gateway_pb2, industrial_gateway_pb2_grpc
from core.infrastructure.rpc.scalar_codec import encode_scalar
from data_generator.domain.model import (
    DataBatch,
    DeliveryResult,
    GeneratorError,
    OutputTarget,
    Scalar,
)
from data_generator.domain.scenario import Scenario

_CLEAR_REJECTION_CODES = frozenset(
    {
        grpc.StatusCode.INVALID_ARGUMENT,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.FAILED_PRECONDITION,
    }
)


class GatewayWriter:
    """Each Gateway is serialized; the global RPC permit follows real call lifetime."""

    def __init__(
        self,
        timeout: float = 3.0,
        max_points: int = 1000,
        max_inflight: int = 1,
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
        self._pending: set[asyncio.Future[Any]] = set()
        self._unsettled: dict[str, asyncio.Future[Any]] = {}
        self._settled_callbacks: set[str] = set()
        self._unsafe: set[str] = set()
        self.rpc_durations: deque[tuple[float, str, float]] = deque(maxlen=4096)
        self.last_native_results: dict[OutputTarget, DeliveryResult] = {}
        self._claims: dict[str, tuple[str, str, tuple[str, ...], str]] = {}
        self._renewers: dict[str, asyncio.Task[None]] = {}
        self._lease_ms = min(60000, max(10000, math.ceil(timeout * 6000)))

    @property
    def unsafe_gateways(self) -> frozenset[str]:
        """Quarantined Gateway IDs for which completion cannot prove safe release."""
        return frozenset(self._unsafe)

    @property
    def unsettled_gateways(self) -> frozenset[str]:
        """Native calls still in progress, including those that timed out logically."""
        return frozenset(
            name
            for name, task in self._unsettled.items()
            if not task.done() or name not in self._settled_callbacks
        )

    async def wait_settled(self, gateway_ids: frozenset[str], timeout: float) -> bool:
        """Wait no longer than the stop deadline; do not cancel native calls."""
        tasks = [
            task
            for name, task in self._unsettled.items()
            if name in gateway_ids and not task.done()
        ]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                return False
        # A completed Task is not released until its accounting callback has run.
        return not bool(gateway_ids.intersection(self.unsafe_gateways | self.unsettled_gateways))

    async def validate(self, scenario: Scenario) -> None:
        """Read the real Gateway catalog; never send data before target validation."""
        expected: dict[str, dict[str, str]] = defaultdict(dict)
        for mapping in scenario.mappings:
            key = (mapping.device_id, mapping.point_id)
            expected[mapping.target.gateway_id][mapping.target.variable_id] = str(
                scenario.points[key]["data_type"]
            )

        async def check(gateway_id: str, expected_types: dict[str, str]) -> None:
            endpoint = scenario.gateways[gateway_id]
            channel = self._channels.get(endpoint)
            if channel is None:
                channel = grpc.aio.insecure_channel(endpoint)
                self._channels[endpoint] = channel
            stub = industrial_gateway_pb2_grpc.IndustrialGatewayServiceStub(channel)
            found: set[str] = set()
            token = ""
            for _ in range(100):
                try:
                    response = await asyncio.wait_for(
                        stub.ListVariables(
                            industrial_gateway_pb2.ListVariablesRequest(
                                gateway_id=gateway_id,
                                page_token=token,
                                page_size=1000,
                            ),
                        ),
                        timeout=self._timeout,
                    )
                except (grpc.RpcError, TimeoutError) as exc:
                    raise GeneratorError(
                        f"Gateway {gateway_id} variable preflight failed: {exc}"
                    ) from exc
                for item in response.variables:
                    required = expected_types.get(item.variable_id)
                    if required is None:
                        continue
                    if item.variable_id in found:
                        raise GeneratorError(
                            f"Gateway {gateway_id} repeated target {item.variable_id}"
                        )
                    if not item.writable or item.data_type != required:
                        raise GeneratorError(
                            f"Gateway {gateway_id} target {item.variable_id}: "
                            f"expected writable {required}, found {item.data_type}"
                        )
                    found.add(item.variable_id)
                token = response.next_page_token
                if not token:
                    break
            else:
                raise GeneratorError(f"Gateway {gateway_id} catalog page limit")
            missing = set(expected_types) - found
            if missing:
                raise GeneratorError(f"Gateway {gateway_id} missing targets: {sorted(missing)}")

        await asyncio.gather(*(check(name, points) for name, points in expected.items()))

    def has_claims(self, owner_id: str) -> bool:
        """Never release in-process ownership while a Gateway still holds our claim."""
        return any(claim[0] == owner_id for claim in self._claims.values())

    async def claim(self, scenario: Scenario, owner_id: str) -> None:
        """Acquire all Gateway groups or roll back every acknowledged group."""
        acquired: list[str] = []
        try:
            for gateway_id in sorted(scenario.gateways):
                names = tuple(sorted(
                    mapping.target.variable_id
                    for mapping in scenario.mappings
                    if mapping.target.gateway_id == gateway_id
                ))
                if not names:
                    continue
                endpoint = scenario.gateways[gateway_id]
                stub = self._stub(endpoint)
                response = await asyncio.wait_for(
                    stub.ClaimTargets(
                        industrial_gateway_pb2.ClaimTargetsRequest(
                            gateway_id=gateway_id, owner_id=owner_id,
                            variable_ids=names, action="ACQUIRE", lease_ms=self._lease_ms,
                        ),
                        timeout=self._timeout,
                    ),
                    timeout=self._timeout,
                )
                if not response.accepted:
                    raise GeneratorError(
                        f"target ownership conflict {gateway_id}: {response.error_code}"
                    )
                self._claims[gateway_id] = (
                    owner_id, response.lease_token, names, endpoint,
                )
                acquired.append(gateway_id)
            for gateway_id in acquired:
                self._renewers[gateway_id] = asyncio.create_task(
                    self._renew(gateway_id)
                )
        except BaseException:
            for gateway_id in acquired:
                await self._release_one(gateway_id)
            raise

    def _stub(
        self, endpoint: str,
    ) -> industrial_gateway_pb2_grpc.IndustrialGatewayServiceStub:
        channel = self._channels.get(endpoint)
        if channel is None:
            channel = grpc.aio.insecure_channel(endpoint)
            self._channels[endpoint] = channel
        return industrial_gateway_pb2_grpc.IndustrialGatewayServiceStub(channel)

    async def _renew(self, gateway_id: str) -> None:
        try:
            while gateway_id in self._claims:
                await asyncio.sleep(self._lease_ms / 3000)
                claim = self._claims.get(gateway_id)
                if claim is None:
                    return
                owner_id, token, names, endpoint = claim
                try:
                    reply = await asyncio.wait_for(
                        self._stub(endpoint).ClaimTargets(
                            industrial_gateway_pb2.ClaimTargetsRequest(
                                gateway_id=gateway_id, owner_id=owner_id,
                                variable_ids=names, action="RENEW",
                                lease_token=token, lease_ms=self._lease_ms,
                            ),
                            timeout=self._timeout,
                        ),
                        timeout=self._timeout,
                    )
                    if not reply.accepted:
                        self._unsafe.add(gateway_id)
                        return
                    # Read-only lease renewal is a controlled re-probe. It
                    # does not replay an earlier UNKNOWN variable update.
                    if gateway_id not in self.unsettled_gateways:
                        self._unsafe.discard(gateway_id)
                except (grpc.RpcError, TimeoutError):
                    self._unsafe.add(gateway_id)
                    # A transient network fault can recover before lease expiry.
                    # Later renewal is fenced by the Gateway's live token.
                    continue
        except asyncio.CancelledError:
            raise

    async def _release_one(self, gateway_id: str) -> bool:
        claim = self._claims.get(gateway_id)
        if claim is None:
            return True
        renewer = self._renewers.pop(gateway_id, None)
        if renewer is not None:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        owner_id, token, names, endpoint = claim
        if gateway_id in self.unsettled_gateways:
            self._unsafe.add(gateway_id)
            return False
        try:
            reply = await asyncio.wait_for(
                self._stub(endpoint).ClaimTargets(
                    industrial_gateway_pb2.ClaimTargetsRequest(
                        gateway_id=gateway_id, owner_id=owner_id,
                        variable_ids=names, action="RELEASE", lease_token=token,
                    ),
                    timeout=self._timeout,
                ),
                timeout=self._timeout,
            )
        except (grpc.RpcError, TimeoutError):
            self._unsafe.add(gateway_id)
            return False
        if not reply.accepted:
            self._unsafe.add(gateway_id)
            return False
        self._claims.pop(gateway_id, None)
        self._unsafe.discard(gateway_id)
        return True

    async def release(self, scenario: Scenario, owner_id: str) -> bool:
        """Release only after source and native I/O settle; fail closed."""
        success = True
        for gateway_id in sorted(scenario.gateways):
            claim = self._claims.get(gateway_id)
            if claim is not None and claim[0] == owner_id:
                success = await self._release_one(gateway_id) and success
        return success

    async def send(self, scenario: Scenario, batch: DataBatch) -> list[DeliveryResult]:
        values = {(point.device_id, point.point_id): point.value for point in batch.points}
        groups: dict[str, list[tuple[OutputTarget, Scalar]]] = defaultdict(list)
        for mapping in scenario.mappings:
            key = (mapping.device_id, mapping.point_id)
            if key in values:
                groups[mapping.target.gateway_id].append((mapping.target, values[key]))

        async def group(
            gateway_id: str, entries: list[tuple[OutputTarget, Scalar]]
        ) -> list[DeliveryResult]:
            outcomes: list[DeliveryResult] = []
            for offset in range(0, len(entries), self._max_points):
                outcomes.extend(
                    await self._send_one(
                        scenario,
                        gateway_id,
                        entries[offset : offset + self._max_points],
                    )
                )
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
                return [
                    DeliveryResult(target, "FAILED", "TARGET_QUARANTINED") for target, _ in entries
                ]
            claim = self._claims.get(gateway_id)
            if claim is None:
                return [DeliveryResult(target, "FAILED", "LEASE_NOT_HELD") for target, _ in entries]
            prior = self._unsettled.get(gateway_id)
            if prior is not None and (
                not prior.done() or gateway_id not in self._settled_callbacks
            ):
                return [DeliveryResult(target, "FAILED", "TARGET_BUSY") for target, _ in entries]
            self._unsettled.pop(gateway_id, None)
            self._settled_callbacks.discard(gateway_id)
            try:
                await asyncio.wait_for(self._available.acquire(), timeout=self._timeout)
            except TimeoutError:
                return [
                    DeliveryResult(target, "FAILED", "BACKPRESSURE_TIMEOUT")
                    for target, _ in entries
                ]
            try:
                endpoint = scenario.gateways[gateway_id]
                if endpoint not in self._channels:
                    self._channels[endpoint] = grpc.aio.insecure_channel(endpoint)
                stub = industrial_gateway_pb2_grpc.IndustrialGatewayServiceStub(
                    self._channels[endpoint]
                )
                request = industrial_gateway_pb2.UpdateVariablesRequest(
                    gateway_id=gateway_id,
                    lease_token=claim[1],
                    values=[
                        industrial_gateway_pb2.VariableUpdate(
                            variable_id=target.variable_id,
                            value=encode_scalar(value),
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

            def on_complete(completed: asyncio.Future[Any]) -> None:
                self._pending.discard(completed)
                self._available.release()
                self.rpc_durations.append(
                    (time.monotonic(), gateway_id, (time.monotonic() - started) * 1000)
                )
                try:
                    reply = completed.result()
                except grpc.RpcError as error:
                    confirmed_rejection = error.code() in _CLEAR_REJECTION_CODES
                    if confirmed_rejection:
                        self._unsafe.discard(gateway_id)
                    else:
                        self._unsafe.add(gateway_id)
                    for target, _ in entries:
                        self.last_native_results[target] = DeliveryResult(
                            target,
                            "FAILED" if confirmed_rejection else "UNKNOWN",
                            str(error.code()),
                        )
                except BaseException:
                    self._unsafe.add(gateway_id)
                    for target, _ in entries:
                        self.last_native_results[target] = DeliveryResult(
                            target,
                            "UNKNOWN",
                            "NATIVE_CALL_UNSETTLED",
                        )
                else:
                    received = {item.variable_id: item for item in reply.results}
                    valid = len(received) == len(reply.results) and set(received) == expected
                    if valid:
                        self._unsafe.discard(gateway_id)
                    else:
                        self._unsafe.add(gateway_id)
                    for target, _ in entries:
                        item = received.get(target.variable_id) if valid else None
                        self.last_native_results[target] = (
                            DeliveryResult(
                                target,
                                "SUCCESS" if item.success else "FAILED",
                                item.error_code,
                                item.message,
                            )
                            if item is not None
                            else DeliveryResult(
                                target,
                                "UNKNOWN",
                                "INVALID_ACK",
                            )
                        )
                self._settled_callbacks.add(gateway_id)

            task.add_done_callback(on_complete)
            try:
                response = await asyncio.wait_for(asyncio.shield(task), self._timeout)
            except grpc.RpcError as error:
                code = error.code()
                if code in _CLEAR_REJECTION_CODES:
                    return [
                        DeliveryResult(target, "FAILED", str(code), str(error))
                        for target, _ in entries
                    ]
                self._unsafe.add(gateway_id)
                return [DeliveryResult(target, "UNKNOWN", str(code)) for target, _ in entries]
            except TimeoutError:
                self._unsafe.add(gateway_id)
                return [DeliveryResult(target, "UNKNOWN", "RPC_TIMEOUT") for target, _ in entries]
            except asyncio.CancelledError:
                self._unsafe.add(gateway_id)
                raise

            received = {item.variable_id: item for item in response.results}
            if len(received) != len(response.results) or set(received) != expected:
                self._unsafe.add(gateway_id)
                return [DeliveryResult(target, "UNKNOWN", "INVALID_ACK") for target, _ in entries]
            results: list[DeliveryResult] = []
            for target, _ in entries:
                item = received.get(target.variable_id)
                if item is None:
                    self._unsafe.add(gateway_id)
                    results.append(DeliveryResult(target, "UNKNOWN", "MISSING_ACK"))
                elif item.success:
                    results.append(DeliveryResult(target, "SUCCESS"))
                else:
                    results.append(
                        DeliveryResult(
                            target,
                            "FAILED",
                            item.error_code,
                            item.message,
                        )
                    )
            return results

    async def drain(self) -> None:
        """Await the native call objects; unsafe results remain isolated."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)

    async def close(self) -> bool:
        """Bound shutdown; an unresolved native call is never reported as confirmed."""
        settled = True
        if self._pending:
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*tuple(self._pending), return_exceptions=True)),
                    timeout=self._timeout * 2,
                )
            except TimeoutError:
                settled = False
                self._unsafe.update(self.unsettled_gateways)
        for renewer in self._renewers.values():
            renewer.cancel()
        if self._renewers:
            await asyncio.gather(*self._renewers.values(), return_exceptions=True)
        self._renewers.clear()
        for channel in self._channels.values():
            await channel.close()
        self._channels.clear()
        return settled and not self._unsafe
