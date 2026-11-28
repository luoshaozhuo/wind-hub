"""Realtime Gateway output through authenticated leases and batch variable updates."""

from __future__ import annotations

import asyncio
import logging
import os
from time import perf_counter

import grpc

from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from contracts.scalar_codec import encode_scalar
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError

logger = logging.getLogger(__name__)

SourceKey = tuple[str, str]
TargetMap = dict[SourceKey, tuple[str, ...]]


class GatewayOutput:
    """One authenticated Gateway lease owner shared across realtime Deliveries."""

    def __init__(
        self,
        *,
        endpoint: str,
        gateway_id: str,
        owner_id: str,
        mappings: TargetMap,
        conversions: dict[str, str] | None = None,
        timeout_ms: int,
        lease_ms: int,
        max_batch_size: int,
    ) -> None:
        self._endpoint = endpoint
        self._gateway_id = gateway_id
        self._owner_id = owner_id
        self._mappings = mappings
        self._conversions = conversions or {}
        self._variable_ids = tuple(
            sorted({value for values in mappings.values() for value in values})
        )
        self._timeout = timeout_ms / 1000
        self._lease_ms = lease_ms
        self._max_batch = max_batch_size
        token = os.environ.get("WIND_HUB_GATEWAY_SINK_TOKEN", "")
        if token and (len(token) < 32 or token.strip() != token):
            raise ConfigurationError("Gateway Sink token is invalid")
        self._metadata = (("authorization", f"Bearer {token}"),) if token else ()
        self._channel: grpc.aio.Channel | None = None
        self._client: gateway_rpc.IndustrialGatewayServiceAsyncStub | None = None
        self._lease_token = ""
        self._renew_task: asyncio.Task[None] | None = None
        self._unhealthy = False

    async def open(self) -> None:
        if self._client is not None:
            return
        if not self._variable_ids:
            raise ConfigurationError("Gateway requires at least one mapped variable")
        channel = grpc.aio.insecure_channel(self._endpoint)
        self._channel = channel
        self._client = gateway_rpc.IndustrialGatewayServiceStub(channel)
        try:
            response = await self._claim("ACQUIRE", "", self._lease_ms)
            if not response.accepted or not response.lease_token:
                detail = response.error_code or "missing lease token"
                raise ConfigurationError(f"Gateway denied the initial target lease: {detail}")
            self._lease_token = response.lease_token
            self._unhealthy = False
            self._renew_task = asyncio.create_task(self._renew(), name="sink:gateway-lease")
        except BaseException:
            await channel.close()
            self._channel = None
            self._client = None
            raise

    async def _claim(self, action: str, token: str, lease_ms: int) -> gateway.ClaimTargetsResponse:
        client = self._client
        if client is None:
            raise RuntimeError("Gateway client is not connected")
        return await client.ClaimTargets(
            gateway.ClaimTargetsRequest(
                gateway_id=self._gateway_id,
                owner_id=self._owner_id,
                variable_ids=self._variable_ids,
                action=action,
                lease_token=token,
                lease_ms=lease_ms,
            ),
            timeout=self._timeout,
            metadata=self._metadata,
        )

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._lease_ms / 3000)
                token = self._lease_token
                if not token:
                    return
                response = await self._claim("RENEW", token, self._lease_ms)
                if not response.accepted or response.lease_token not in ("", token):
                    self._unhealthy = True
                    self._lease_token = ""
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Gateway lease renewal became uncertain", exc_info=True)
            self._unhealthy = True
            self._lease_token = ""

    async def close(self) -> None:
        task, self._renew_task = self._renew_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        token, self._lease_token = self._lease_token, ""
        if token:
            try:
                await self._claim("RELEASE", token, 0)
            except Exception:
                logger.warning("Gateway lease release uncertain; target TTL will expire")
        channel, self._channel = self._channel, None
        self._client = None
        self._unhealthy = True
        if channel is not None:
            await channel.close()

    def _target_value(self, variable_id: str, value: float | int | bool | str) -> float | int | bool | str:
        conversion = self._conversions.get(variable_id)
        if conversion is None:
            return value
        if conversion == "int01_to_bool":
            if type(value) is not int or value not in (0, 1):
                raise ValueError("int01_to_bool requires integer 0 or 1")
            return bool(value)
        raise ValueError(f"unsupported Gateway target conversion: {conversion}")

    async def write_batch(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome:
        client = self._client
        if client is None or not self._lease_token or self._unhealthy:
            return BatchOutcome(delivered=(), settled=(), error="GATEWAY_LEASE_UNAVAILABLE")
        if not samples:
            return BatchOutcome(delivered=(), settled=())
        if len(samples) > self._max_batch:
            return BatchOutcome(delivered=(), settled=(), error="GATEWAY_BATCH_LIMIT")
        updates: list[gateway.VariableUpdate] = []
        expected: dict[str, int] = {}
        excluded: set[int] = set()
        rejected: set[int] = set()
        for index, sample in enumerate(samples):
            targets = self._mappings.get((sample.device_id, sample.point_id))
            if not targets or sample.value is None:
                excluded.add(index)
                continue
            try:
                converted = [
                    (variable_id, self._target_value(variable_id, sample.value))
                    for variable_id in targets
                ]
            except ValueError:
                rejected.add(index)
                logger.warning(
                    "Rejected invalid Gateway mapping conversion for %s:%s",
                    sample.device_id,
                    sample.point_id,
                )
                continue
            for variable_id, value in converted:
                updates.append(
                    gateway.VariableUpdate(
                        variable_id=variable_id,
                        value=encode_scalar(value),
                    )
                )
                expected[variable_id] = index
        if len(updates) > self._max_batch:
            return BatchOutcome(delivered=(), settled=(), error="GATEWAY_BATCH_LIMIT")
        if not updates:
            return BatchOutcome(
                delivered=(),
                settled=tuple(sorted(excluded | rejected)),
                error="GATEWAY_CONVERSION_INVALID" if rejected else "GATEWAY_NO_VALID_TARGETS",
                permanent=tuple(sorted(rejected)),
            )
        started = perf_counter()
        try:
            response = await client.UpdateVariables(
                gateway.UpdateVariablesRequest(
                    gateway_id=self._gateway_id,
                    values=updates,
                    lease_token=self._lease_token,
                ),
                timeout=self._timeout,
                metadata=self._metadata,
            )
        except Exception:
            # A transport error may hide partial variable updates. Do not retry blindly.
            self._unhealthy = True
            return BatchOutcome(
                delivered=(),
                settled=tuple(sorted(excluded)),
                error="GATEWAY_UPDATE_UNKNOWN",
            )
        succeeded: set[str] = set()
        failed: set[str] = set()
        for result in response.results:
            if result.variable_id not in expected or result.variable_id in succeeded | failed:
                self._unhealthy = True
                return BatchOutcome(
                    delivered=(),
                    settled=tuple(sorted(excluded)),
                    error="GATEWAY_RESULT_MISMATCH",
                )
            (succeeded if result.success else failed).add(result.variable_id)
        delivered: list[int] = []
        settled: set[int] = set(excluded | rejected)
        for index, sample in enumerate(samples):
            targets = self._mappings.get((sample.device_id, sample.point_id), ())
            if targets and all(target in succeeded for target in targets):
                delivered.append(index)
                settled.add(index)
            elif targets and any(target in failed for target in targets):
                settled.add(index)
        elapsed_ms = (perf_counter() - started) * 1000
        return BatchOutcome(
            delivered=tuple(delivered),
            settled=tuple(sorted(settled)),
            error=(
                "GATEWAY_CONVERSION_INVALID"
                if rejected
                else "GATEWAY_PARTIAL" if len(delivered) != len(samples) else ""
            ),
            permanent=tuple(sorted(rejected)),
            successful_attempts=1 if succeeded else 0,
            failed_attempts=1 if failed else 0,
            successful_latencies_ms=(elapsed_ms,) if succeeded else (),
            successful_latency_weights=((elapsed_ms, len(delivered)),) if delivered else (),
        )
