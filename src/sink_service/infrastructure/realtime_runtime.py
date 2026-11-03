"""Realtime NATS ingress and per-Sink shared output resource ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

import nats
import yaml
from contracts.sample_message import SampleMessage
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from sink_service.application.config import template_path
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import RealtimeEngine
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot, config_sources
from sink_service.infrastructure.realtime_adapters import (
    KafkaOutput,
    ModbusOutput,
    RedisOutput,
)

logger = logging.getLogger(__name__)


def _template(snapshot: ValidatedSnapshot, family: str, name: str, version: str) -> dict[str, Any]:
    _, root = config_sources(snapshot.service)
    path = template_path(root, family, name, version)
    for item in snapshot.files:
        if item.path == path:
            document = yaml.safe_load(item.content)
            if not isinstance(document, dict):
                raise ConfigurationError(f"{path}: invalid template document")
            return document
    raise ConfigurationError(f"missing template {path}")


class NatsRealtimeIngress:
    """One NATS Core subscriber for all running realtime Deliveries in a process."""

    def __init__(self, *, servers: list[str], subject_prefix: str) -> None:
        self._servers = servers
        self._subject = subject_prefix.rstrip(".") + ".>"
        self._client: Client | None = None
        self._subscription: Subscription | None = None
        self._engines: dict[str, RealtimeEngine] = {}
        self._lock = asyncio.Lock()
        self.last_error = ""

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
                try:
                    subscription = await client.subscribe(
                        self._subject,
                        cb=self._on_message,
                        pending_msgs_limit=8192,
                        pending_bytes_limit=4 * 1024 * 1024,
                    )
                except Exception:
                    await client.close()
                    raise
                self._client = client
                self._subscription = subscription
            self._engines[delivery_id] = engine

    async def remove(self, delivery_id: str) -> None:
        async with self._lock:
            self._engines.pop(delivery_id, None)
            if self._engines:
                return
            client, self._client = self._client, None
            self._subscription = None
            if client is not None:
                try:
                    await asyncio.wait_for(client.drain(), timeout=3)
                finally:
                    await client.close()

    async def _on_message(self, msg: Msg) -> None:
        try:
            sample = SampleMessage.decode(msg.data, "realtime")
            if msg.subject != sample.subject("realtime"):
                self.last_error = "NATS subject/source identity mismatch"
                return
        except (ValueError, UnicodeError) as exc:
            self.last_error = str(exc)
            logger.warning("Sink realtime message rejected: %s", exc)
            return
        for engine in tuple(self._engines.values()):
            engine.offer(sample)


class _SinkOutput:
    """An adapter is opened once for a stable configuration and reference-counted."""

    def __init__(self, adapter: Any, signature: str) -> None:
        self.adapter = adapter
        self.signature = signature
        self.references = 0


class SinkOutputPool:
    """Share one physical Sink between multiple Delivery owners in one process."""

    def __init__(self) -> None:
        self._entries: dict[str, _SinkOutput] = {}
        self._lock = asyncio.Lock()

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
            )
        elif kind == "kafka":
            adapter = KafkaOutput(
                servers=list(connection["bootstrap_servers"]),
                topic=output["topic"],
                timeout_ms=int(output["write_timeout_ms"]),
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
                current = _SinkOutput(adapter, signature)
                self._entries[sink_id] = current
            elif current.signature != signature:
                raise ConfigurationError(
                    f"Sink {sink_id} still in use with a different binding"
                )
            current.references += 1
            return current.adapter

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

    @property
    def last_error(self) -> str:
        if self._engine is None:
            return ""
        if not self._ingress.connected:
            return "NATS realtime source disconnected"
        return self._engine.last_error or self._ingress.last_error

    async def start(self) -> None:
        if self._engine is not None:
            return
        sink = await self._pool.acquire(self._sink_id, self._snapshot)
        instance = self._snapshot.deliveries[self._id]
        template = _template(
            self._snapshot, "deliveries",
            instance["template_id"], instance["template_version"],
        )
        output = template["output"]
        staleness = template.get("staleness", {})
        source = instance["source"]
        engine = RealtimeEngine(
            writer=sink,
            wind_farm_id=source["wind_farm_id"],
            collector_id=source["collector_id"],
            devices=frozenset(source["devices"]),
            points=frozenset(source["points"]),
            policy=output["policy"],
            period_ms=output["period_ms"],
            max_points=self._snapshot.service["runtime"]["max_pending_points"],
            max_age_ms=staleness.get("max_age_ms"),
            absolute_threshold=output.get("absolute_threshold", 0.0),
            relative_threshold=output.get("relative_threshold", 0.0),
        )
        try:
            await engine.start()
            await self._ingress.add(self._id, engine)
        except Exception:
            await engine.stop()
            await self._pool.release(self._sink_id)
            raise
        self._engine = engine

    async def stop(self) -> None:
        engine = self._engine
        if engine is None:
            return
        # Keep ownership until all work and the physical connection are settled.
        await self._ingress.remove(self._id)
        await engine.stop()
        await self._pool.release(self._sink_id)
        self._engine = None


class RealtimeFactory:
    """Process-level assembler; reliable ports remain fail-closed until stage three."""

    def __init__(self) -> None:
        self._pool = SinkOutputPool()
        self._ingress: NatsRealtimeIngress | None = None

    def __call__(
        self, definition: DeliveryDefinition, snapshot: ValidatedSnapshot,
    ) -> RealtimeDeliveryPort:
        if definition.channel != "realtime":
            raise ConfigurationError("reliable data plane is reserved for stage three")
        if self._ingress is None:
            nats_config = snapshot.service["nats"]
            self._ingress = NatsRealtimeIngress(
                servers=list(nats_config["servers"]),
                subject_prefix=nats_config["realtime_subject"],
            )
        return RealtimeDeliveryPort(definition, snapshot, self._pool, self._ingress)
