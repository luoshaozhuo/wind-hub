"""NATS Core ingress adapter for Sink realtime deliveries."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime

import nats
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from contracts.sample_batch import SampleBatch
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.realtime import RealtimeEngine
from sink_service.domain.config import ConfigurationError

logger = logging.getLogger(__name__)


class NatsRealtimeIngress:
    """One NATS Core subscriber for all running realtime Deliveries in a process."""

    def __init__(
        self,
        *,
        servers: list[str],
        subject_prefix: str,
        max_samples: int,
        max_bytes: int,
    ) -> None:
        self._servers = servers
        self._subject_prefix = subject_prefix.rstrip(".")
        self._max_samples = max_samples
        self._max_bytes = max_bytes
        self._client: Client | None = None
        self._subscriptions: dict[tuple[str, str], Subscription] = {}
        self._engines: dict[str, RealtimeEngine] = {}
        self._routes: dict[tuple[str, str], dict[str, RealtimeEngine]] = {}
        self._target_cache: OrderedDict[tuple[str, str, str, str], tuple[RealtimeEngine, ...]] = (
            OrderedDict()
        )
        self._lock = asyncio.Lock()
        self.last_error = ""
        self.received_batches_total = 0
        self.received_values_total = 0
        self.rejected_batches_total = 0
        self.routed_values_total = 0

    def counters(self) -> dict[str, float]:
        """Return process-local ingress counts, not Delivery fan-out counts."""
        return {
            "input_batches_total": float(self.received_batches_total),
            "input_values_total": float(self.received_values_total),
            "rejected_batches_total": float(self.rejected_batches_total),
            "routed_values_total": float(self.routed_values_total),
        }

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def add(self, delivery_id: str, engine: RealtimeEngine) -> None:
        async with self._lock:
            if delivery_id in self._engines:
                raise ConfigurationError(f"duplicate NATS subscriber: {delivery_id}")
            if self._client is None:
                client = await nats.connect(
                    servers=self._servers,
                    allow_reconnect=True,
                    max_reconnect_attempts=-1,
                    connect_timeout=2,
                    reconnect_time_wait=1,
                    pending_size=1024 * 1024,
                )
                self._client = client
            source = engine.source_identity
            if source not in self._subscriptions:
                assert self._client is not None
                subject = f"{self._subject_prefix}.{source[0]}.{source[1]}.>"
                try:
                    subscription = await self._client.subscribe(
                        subject,
                        cb=self._on_message,
                        pending_msgs_limit=8192,
                        pending_bytes_limit=4 * 1024 * 1024,
                    )
                except BaseException:
                    if not self._engines:
                        orphan, self._client = self._client, None
                        if orphan is not None:
                            await orphan.close()
                    raise
                self._subscriptions[source] = subscription
            self._engines[delivery_id] = engine
            self._routes.setdefault(engine.source_identity, {})[delivery_id] = engine
            self._target_cache.clear()

    async def remove(self, delivery_id: str) -> None:
        async with self._lock:
            engine = self._engines.pop(delivery_id, None)
            if engine is not None:
                route = self._routes[engine.source_identity]
                route.pop(delivery_id)
                if not route:
                    del self._routes[engine.source_identity]
                    subscription = self._subscriptions.pop(engine.source_identity)
                    await subscription.unsubscribe()
                self._target_cache.clear()
            if self._engines:
                return
            client, self._client = self._client, None
            if client is not None:
                try:
                    await asyncio.wait_for(client.drain(), timeout=3)
                finally:
                    await client.close()

    async def _on_message(self, msg: Msg) -> None:
        """Decode one source batch, then route only matching measurement points."""
        try:
            batch = SampleBatch.decode(
                msg.data,
                mode="realtime",
                subject=msg.subject,
                max_samples=self._max_samples,
                max_bytes=self._max_bytes,
            )
        except (ValueError, UnicodeError) as exc:
            self.last_error = str(exc)
            self.rejected_batches_total += 1
            logger.warning("Sink realtime batch rejected: %s", exc)
            return

        self.received_batches_total += 1
        self.received_values_total += len(batch.samples)
        source = (batch.wind_farm_id, batch.collector_id)
        routes = self._routes.get(source)
        if not routes:
            return
        now = datetime.now(UTC)
        cache_limit = max(16384, len(self._engines) * 4096)
        for item in batch.samples:
            identity = (
                batch.wind_farm_id,
                batch.collector_id,
                batch.device_id,
                item.point_id,
            )
            targets = self._target_cache.get(identity)
            if targets is None:
                targets = tuple(
                    engine
                    for engine in routes.values()
                    if engine.accepts(batch.device_id, item.point_id)
                )
                if len(self._target_cache) >= cache_limit:
                    self._target_cache.popitem(last=False)
                self._target_cache[identity] = targets
            else:
                self._target_cache.move_to_end(identity)
            if targets:
                sample = DeliveryPoint.from_batch(batch, item)
                for engine in targets:
                    if engine.offer(sample, now=now, routed=True):
                        self.routed_values_total += 1
