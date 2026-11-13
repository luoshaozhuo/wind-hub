"""Realtime Gateway output through authenticated leases and batch variable updates."""

from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter

import grpc

from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from contracts.scalar_codec import encode_scalar
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.secrets import resolve_secret

logger = logging.getLogger(__name__)

SourceKey = tuple[str, str]
TargetMap = dict[SourceKey, tuple[str, ...]]


class GatewayOutput:
    """One authenticated Gateway lease owner shared across realtime Deliveries."""

    def __init__(
        self,
        *,
        endpoint: str,
        credential_ref: str,
        gateway_id: str,
        owner_id: str,
        mappings: TargetMap,
        timeout_ms: int,
        lease_ms: int,
        max_batch_size: int,
    ) -> None:
        self._endpoint = endpoint
        self._credential_ref = credential_ref
        self._gateway_id = gateway_id
        self._owner_id = owner_id
        self._mappings = mappings
        self._variable_ids = tuple(
            sorted({value for values in mappings.values() for value in values})
        )
        self._timeout = timeout_ms / 1000
        self._lease_ms = lease_ms
        self._max_batch = max_batch_size
        self._channel: grpc.aio.Channel | None = None
        self._client: gateway_rpc.IndustrialGatewayServiceAsyncStub | None = None
        self._lease_token = ""
        self._renew_task: asyncio.Task[None] | None = None
        self._unhealthy = False

    def _credentials(self) -> grpc.ChannelCredentials:
        secret = resolve_secret(self._credential_ref)
        if secret is None:
            raise ConfigurationError("Gateway output requires an explicit mTLS Secret")
        try:
            document = json.loads(secret)
            if not isinstance(document, dict):
                raise ValueError("not a mapping")
            keys = ("ca_pem", "client_cert_pem", "client_key_pem")
            if any(
                not isinstance(document.get(key), str) or not document[key]
                for key in keys
            ):
                raise ValueError("missing certificate material")
            return grpc.ssl_channel_credentials(
                root_certificates=document["ca_pem"].encode("utf-8"),
                private_key=document["client_key_pem"].encode("utf-8"),
                certificate_chain=document["client_cert_pem"].encode("utf-8"),
            )
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid Gateway mTLS Secret payload") from exc

    async def open(self) -> None:
        if self._client is not None:
            return
        if not self._variable_ids:
            raise ConfigurationError("Gateway requires at least one mapped variable")
        channel = grpc.aio.secure_channel(self._endpoint, self._credentials())
        self._channel = channel
        self._client = gateway_rpc.IndustrialGatewayServiceStub(channel)
        try:
            response = await self._claim("ACQUIRE", "", self._lease_ms)
            if not response.accepted or not response.lease_token:
                raise ConfigurationError("Gateway denied the initial target lease")
            self._lease_token = response.lease_token
            self._unhealthy = False
            self._renew_task = asyncio.create_task(self._renew(), name="sink:gateway-lease")
        except BaseException:
            await channel.close()
            self._channel = None
            self._client = None
            raise

    async def _claim(
        self, action: str, token: str, lease_ms: int
    ) -> gateway.ClaimTargetsResponse:
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
        for index, sample in enumerate(samples):
            targets = self._mappings.get((sample.device_id, sample.point_id))
            if not targets or sample.value is None:
                excluded.add(index)
                continue
            for variable_id in targets:
                updates.append(
                    gateway.VariableUpdate(
                        variable_id=variable_id,
                        value=encode_scalar(sample.value),
                    )
                )
                expected[variable_id] = index
        if len(updates) > self._max_batch:
            return BatchOutcome(delivered=(), settled=(), error="GATEWAY_BATCH_LIMIT")
        if not updates:
            return BatchOutcome(
                delivered=(),
                settled=tuple(sorted(excluded)),
                error="GATEWAY_NO_VALID_TARGETS",
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
        settled: set[int] = set(excluded)
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
            error="GATEWAY_PARTIAL" if len(delivered) != len(samples) else "",
            successful_attempts=1 if succeeded else 0,
            failed_attempts=1 if failed else 0,
            successful_latencies_ms=(elapsed_ms,) if succeeded else (),
            successful_latency_weights=((elapsed_ms, len(delivered)),) if delivered else (),
        )
