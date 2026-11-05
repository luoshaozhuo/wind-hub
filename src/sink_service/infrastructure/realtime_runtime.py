"""Realtime NATS ingress and per-Sink shared output resource ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict, deque
from datetime import UTC, datetime
from typing import Any

import nats
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from contracts.sample_batch import SampleBatch
from contracts.sample_message import SampleMessage
from sink_service.application.config import template_document as _template
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import BatchOutcome, RealtimeEngine
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot
from sink_service.infrastructure.realtime_adapters import (
    KafkaOutput,
    ModbusOutput,
    RedisOutput,
)

logger = logging.getLogger(__name__)


class NatsRealtimeIngress:
    """One NATS Core subscriber for all running realtime Deliveries in a process."""

    def __init__(self, *, servers: list[str], subject_prefix: str) -> None:
        self._servers = servers
        self._subject_prefix = subject_prefix.rstrip(".")
        self._client: Client | None = None
        self._subscriptions: dict[tuple[str, str], Subscription] = {}
        self._engines: dict[str, RealtimeEngine] = {}
        self._routes: dict[tuple[str, str], dict[str, RealtimeEngine]] = {}
        self._target_cache: OrderedDict[
            tuple[str, str, str, str], tuple[RealtimeEngine, ...]
        ] = OrderedDict()
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
                self._subscriptions[source] = await self._client.subscribe(
                    subject,
                    cb=self._on_message,
                    pending_msgs_limit=8192,
                    pending_bytes_limit=4 * 1024 * 1024,
                )
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
                msg.data, mode="realtime", subject=msg.subject
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
        for sample in batch.to_samples():
            identity = (
                batch.wind_farm_id,
                batch.collector_id,
                batch.device_id,
                sample.point_id,
            )
            targets = self._target_cache.get(identity)
            if targets is None:
                targets = tuple(
                    engine for engine in routes.values()
                    if engine.accepts(batch.device_id, sample.point_id)
                )
                if len(self._target_cache) >= cache_limit:
                    self._target_cache.popitem(last=False)
                self._target_cache[identity] = targets
            else:
                self._target_cache.move_to_end(identity)
            for engine in targets:
                if engine.offer(sample, now=now, routed=True):
                    self.routed_values_total += 1


class _SinkOutput:
    """Shared adapter, ownership count, and a physical Sink concurrency cap."""

    def __init__(self, adapter: Any, signature: str, capacity: int) -> None:
        self.adapter = adapter
        self.signature = signature
        self.references = 0
        self.quota = asyncio.Semaphore(capacity)


class _QuotaWriter:
    """Keep the global/Sink quota while native I/O is not settled."""

    def __init__(
        self,
        adapter: Any,
        global_quota: asyncio.Semaphore,
        sink_quota: asyncio.Semaphore,
        timeout_ms: int,
    ) -> None:
        self._adapter = adapter
        self._global_quota = global_quota
        self._sink_quota = sink_quota
        self._timeout = timeout_ms / 1000
        self._pending: asyncio.Task[BatchOutcome] | None = None

    def _finish(self, task: asyncio.Task[BatchOutcome]) -> None:
        if not task.cancelled():
            task.exception()
        self._sink_quota.release()
        self._global_quota.release()

    @property
    def io_inflight(self) -> bool:
        task = self._pending
        return task is not None and not task.done()

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        pending = self._pending
        if pending is not None and not pending.done():
            return BatchOutcome((), (), "previous Sink I/O remains unsettled")
        try:
            await asyncio.wait_for(self._global_quota.acquire(), timeout=self._timeout)
        except TimeoutError:
            return BatchOutcome((), (), "global I/O quota exhausted")
        try:
            await asyncio.wait_for(self._sink_quota.acquire(), timeout=self._timeout)
        except BaseException:
            self._global_quota.release()
            raise
        try:
            task = asyncio.create_task(self._adapter.write_batch(samples))
        except BaseException:
            self._sink_quota.release()
            self._global_quota.release()
            raise
        self._pending = task
        task.add_done_callback(self._finish)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
        except TimeoutError:
            return BatchOutcome((), (), "Sink I/O result uncertain; waiting for settlement")

    async def settle(self) -> BatchOutcome | None:
        """Wait for native settlement even after the caller's observation timeout."""
        task = self._pending
        if task is None:
            return None
        try:
            return await asyncio.shield(task)
        except Exception as exc:
            return BatchOutcome((), (), f'underlying I/O failed: {exc}')


def template_capabilities(snapshot: ValidatedSnapshot, sink_id: str) -> dict[str, Any]:
    sink = snapshot.sinks[sink_id]
    template = _template(snapshot, "sinks", sink["template_id"], sink["template_version"])
    return template["capabilities"]


class SinkOutputPool:
    """Share one physical Sink between multiple Delivery owners in one process."""

    def __init__(self) -> None:
        self._entries: dict[str, _SinkOutput] = {}
        self._lock = asyncio.Lock()
        self._quota: asyncio.Semaphore | None = None

    @staticmethod
    def _build(sink_id: str, snapshot: ValidatedSnapshot) -> tuple[Any, str]:
        sink = snapshot.sinks[sink_id]
        template = _template(
            snapshot, "sinks", sink["template_id"], sink["template_version"]
        )
        connection = sink["connection"]
        output = template["output"]
        kind = template["kind"]
        config_signature: dict[str, Any] = {
            "sink": sink, "template": template,
        }
        if kind == "redis":
            adapter = RedisOutput(
                host=connection["host"],
                port=int(connection["port"]),
                namespace=output["namespace"],
                timeout_ms=int(output["write_timeout_ms"]),
                credential_ref=connection.get("credential_ref", ""),
            )
        elif kind == "kafka":
            adapter = KafkaOutput(
                servers=list(connection["bootstrap_servers"]),
                topic=output["topic"],
                timeout_ms=int(output["write_timeout_ms"]),
                credential_ref=connection.get("credential_ref", ""),
            )
        elif kind == "modbus_tcp":
            points: set[tuple[str, str, str, str]] = set()
            for delivery in snapshot.deliveries.values():
                if delivery["sink_id"] != sink_id:
                    continue
                source = delivery["source"]
                devices, names = source["devices"], source["points"]
                if not devices or not names:
                    raise ConfigurationError("Modbus requires explicit finite point mapping")
                for device_id in devices:
                    for point_id in names:
                        key = (
                            source["wind_farm_id"], source["collector_id"], device_id, point_id,
                        )
                        if key in points:
                            raise ConfigurationError(f"duplicate Modbus point {key}")
                        points.add(key)
            sorted_points = tuple(sorted(points))
            config_signature["points"] = sorted_points
            adapter = ModbusOutput(
                host=connection["listen_host"],
                port=int(connection["port"]),
                unit_id=int(output["unit_id"]),
                points=sorted_points,
                value_start=int(output["register_start"]),
                quality_start=int(output["quality_register_start"]),
            )
        else:
            raise ConfigurationError(f"unsupported realtime Sink type: {kind}")
        signature = hashlib.sha256(
            json.dumps(config_signature, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return adapter, signature

    async def acquire(self, sink_id: str, snapshot: ValidatedSnapshot) -> Any:
        async with self._lock:
            adapter, signature = self._build(sink_id, snapshot)
            current = self._entries.get(sink_id)
            if current is None:
                await adapter.open()
                capabilities = template_capabilities(snapshot, sink_id)
                limit = min(
                    snapshot.service["runtime"]["max_inflight_per_sink"],
                    capabilities["max_safe_inflight"],
                )
                if not capabilities["supports_concurrent_write"]:
                    limit = 1
                current = _SinkOutput(adapter, signature, limit)
                self._entries[sink_id] = current
            elif current.signature != signature:
                raise ConfigurationError(
                    f"Sink {sink_id} still in use with a different binding"
                )
            current.references += 1
            if self._quota is None:
                self._quota = asyncio.Semaphore(
                    snapshot.service["runtime"]["max_inflight_global"]
                )
            return _QuotaWriter(
                current.adapter,
                self._quota,
                current.quota,
                snapshot.service["runtime"]["io_wait_timeout_ms"],
            )

    async def release(self, sink_id: str) -> None:
        async with self._lock:
            current = self._entries[sink_id]
            if current.references > 1:
                current.references -= 1
                return
            await current.adapter.close()
            del self._entries[sink_id]


class RealtimeDeliveryPort:
    """A real Delivery owns NATS registration and latest-value output scheduling."""

    def __init__(
        self,
        definition: DeliveryDefinition,
        snapshot: ValidatedSnapshot,
        pool: SinkOutputPool,
        ingress: NatsRealtimeIngress,
    ) -> None:
        self._id = definition.delivery_id
        self._sink_id = definition.sink_id
        self._snapshot = snapshot
        self._pool = pool
        self._ingress = ingress
        self._engine: RealtimeEngine | None = None
        self._writer: _QuotaWriter | None = None
        self._stopping = False
        self._retained: dict[str, float] = {}
        self._retained_latency: deque[tuple[datetime, float]] = deque(maxlen=50000)
        self._retained_overflow: datetime | None = None

    def inspect(self) -> dict[str, str]:
        """Return cached, non-invasive transport and ownership evidence."""
        return {
            'source': 'CONNECTED' if self._ingress.connected else 'DISCONNECTED',
            'delivery': 'RUNNING' if self._engine is not None else 'STOPPED',
            'sink': 'OWNED' if self._writer is not None else 'NOT_ATTACHED',
            'io_inflight': str(
                self._writer.io_inflight if self._writer is not None else False
            ),
            'target': 'NOT_PROBED',
        }
    @property
    def metrics(self) -> dict[str, float]:
        totals = dict(self._retained)
        engine = self._engine
        if engine is not None:
            for name, value in (
                ('received_total', engine.received_total),
                ('dropped_total', engine.dropped_total),
                ('delivered_total', engine.delivered_total),
                ('failed_total', engine.failed_total),
                ('write_success_total', engine.write_success_total),
            ):
                totals[name] = totals.get(name, 0.0) + float(value)
        totals['io_inflight'] = float(
            self._writer.io_inflight if self._writer is not None else False
        )
        return totals

    @property
    def latency_samples(self) -> tuple[tuple[datetime, float], ...]:
        active = self._engine
        return tuple(self._retained_latency) + (
            tuple(active.latency_samples) if active is not None else ()
        )

    @property
    def latency_overflow_at(self) -> datetime | None:
        candidates = [self._retained_overflow]
        if self._engine is not None:
            candidates.append(self._engine.latency_overflow_at)
        return max((time for time in candidates if time is not None), default=None)
    @property
    def last_error(self) -> str:
        if self._engine is None:
            return ""
        if not self._ingress.connected:
            return "NATS realtime source disconnected"
        return self._engine.last_error

    async def start(self) -> None:
        if self._stopping:
            raise ConfigurationError("previous Delivery stop has not safely settled")
        if self._engine is not None:
            return
        sink = await self._pool.acquire(self._sink_id, self._snapshot)
        engine: RealtimeEngine | None = None
        try:
            instance = self._snapshot.deliveries[self._id]
            template = _template(
                self._snapshot, 'deliveries',
                instance['template_id'], instance['template_version'],
            )
            output = template['output']
            staleness = template.get('staleness', {})
            source = instance['source']
            engine = RealtimeEngine(
                writer=sink,
                wind_farm_id=source['wind_farm_id'],
                collector_id=source['collector_id'],
                devices=frozenset(source['devices']),
                points=frozenset(source['points']),
                policy=output['policy'],
                period_ms=output['period_ms'],
                max_points=self._snapshot.service['runtime']['max_pending_points'],
                max_age_ms=staleness.get('max_age_ms'),
                absolute_threshold=output.get('absolute_threshold', 0.0),
                relative_threshold=output.get('relative_threshold', 0.0),
                preserve_upstream_sample=(
                    _template(
                        self._snapshot,
                        "sinks",
                        self._snapshot.sinks[self._sink_id]["template_id"],
                        self._snapshot.sinks[self._sink_id]["template_version"],
                    )["kind"] == "kafka"
                ),
            )
            await engine.start()
            await self._ingress.add(self._id, engine)
        except BaseException:
            try:
                if engine is not None:
                    await engine.stop()
            finally:
                await self._pool.release(self._sink_id)
            raise
        self._engine = engine
        self._writer = sink

    async def stop(self) -> None:
        engine = self._engine
        if engine is None:
            return
        self._stopping = True
        # Keep ownership until all work and the physical connection are settled.
        await self._ingress.remove(self._id)
        await engine.stop()
        if self._writer is not None:
            await self._writer.settle()
        await self._pool.release(self._sink_id)
        for name, value in (
            ('received_total', engine.received_total),
            ('dropped_total', engine.dropped_total),
            ('delivered_total', engine.delivered_total),
            ('failed_total', engine.failed_total),
            ('write_success_total', engine.write_success_total),
        ):
            self._retained[name] = self._retained.get(name, 0.0) + float(value)
        for item in engine.latency_samples:
            if len(self._retained_latency) == self._retained_latency.maxlen:
                self._retained_overflow = item[0]
            self._retained_latency.append(item)
        if engine.latency_overflow_at is not None:
            if (
                self._retained_overflow is None
                or engine.latency_overflow_at > self._retained_overflow
            ):
                self._retained_overflow = engine.latency_overflow_at
        self._engine = None
        self._writer = None
        self._stopping = False


class RealtimeFactory:
    """Process-local assembler for Realtime Delivery ports."""

    def __init__(self) -> None:
        self._pool = SinkOutputPool()
        self._ingress: NatsRealtimeIngress | None = None

    def ingress_counters(self) -> dict[str, float]:
        ingress = self._ingress
        return ingress.counters() if ingress is not None else {}

    def __call__(
        self, definition: DeliveryDefinition, snapshot: ValidatedSnapshot,
    ) -> RealtimeDeliveryPort:
        if definition.channel != "realtime":
            raise ConfigurationError("RealtimeFactory cannot own reliable Delivery")
        if self._ingress is None:
            nats_config = snapshot.service["nats"]
            self._ingress = NatsRealtimeIngress(
                servers=list(nats_config["servers"]),
                subject_prefix=nats_config["realtime_subject"],
            )
        return RealtimeDeliveryPort(definition, snapshot, self._pool, self._ingress)
