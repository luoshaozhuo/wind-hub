"""Realtime NATS ingress and per-Sink shared output resource ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from sink_service.application.config import template_document as _template
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.output_pool import SharedSinkOutputPool
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.latency_window import HistogramSnapshot, LatencyWindow
from sink_service.application.realtime import RealtimeEngine
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot
from sink_service.infrastructure.adapters.inbound.nats_consumer import NatsRealtimeIngress
from sink_service.infrastructure.adapters.outbound.gateway import GatewayOutput
from sink_service.infrastructure.adapters.outbound.realtime_adapters import (
    KafkaOutput,
    ModbusOutput,
    RedisOutput,
)


class SinkOutputPool(SharedSinkOutputPool):
    """Infrastructure composition of the concrete output adapter factory."""

    def __init__(self) -> None:
        super().__init__(self._build)

    @staticmethod
    def _build(sink_id: str, snapshot: ValidatedSnapshot) -> tuple[Any, str]:
        sink = snapshot.sinks[sink_id]
        template = _template(snapshot, "sinks", sink["template_id"], sink["template_version"])
        connection = sink["connection"]
        output = template["output"]
        kind = template["kind"]
        config_signature: dict[str, Any] = {
            "sink": sink,
            "template": template,
        }
        adapter: RedisOutput | KafkaOutput | ModbusOutput | GatewayOutput
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
                linger_ms=output.get("linger_ms", 0),
                max_batch_size=output.get("max_batch_size", 131072),
            )
        elif kind == "gateway":
            mapping: dict[tuple[str, str], list[str]] = {}
            seen_targets: set[str] = set()
            for delivery in snapshot.deliveries.values():
                if delivery["sink_id"] != sink_id:
                    continue
                for item in delivery["target_mappings"]:
                    variable_id = item["variable_id"]
                    if variable_id in seen_targets:
                        raise ConfigurationError(f"duplicate Gateway variable: {variable_id}")
                    seen_targets.add(variable_id)
                    key = (item["source_device_id"], item["source_point_id"])
                    mapping.setdefault(key, []).append(variable_id)
            fixed_mapping = {key: tuple(values) for key, values in mapping.items()}
            config_signature["gateway_mappings"] = [
                (key, values) for key, values in sorted(fixed_mapping.items())
            ]
            adapter = GatewayOutput(
                endpoint=connection["endpoint"],
                credential_ref=connection["credential_ref"],
                gateway_id=connection["gateway_id"],
                owner_id=f"{snapshot.service['deployment_id']}:{sink_id}",
                mappings=fixed_mapping,
                timeout_ms=int(output["write_timeout_ms"]),
                lease_ms=int(output["lease_ms"]),
                max_batch_size=int(output["max_batch_size"]),
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
                        point_key = (
                            source["wind_farm_id"],
                            source["collector_id"],
                            device_id,
                            point_id,
                        )
                        if point_key in points:
                            raise ConfigurationError(f"duplicate Modbus point {point_key}")
                        points.add(point_key)
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
            raise ConfigurationError(f"unsupported Sink type: {kind}")
        signature = hashlib.sha256(
            json.dumps(config_signature, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return adapter, signature

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
        self._retained_latency = LatencyWindow(
            window_ms=snapshot.service["metrics"]["metrics_window_ms"]
        )

    def inspect(self) -> dict[str, str]:
        """Return cached, non-invasive transport and ownership evidence."""
        return {
            "source": "CONNECTED" if self._ingress.connected else "DISCONNECTED",
            "delivery": "RUNNING" if self._engine is not None else "STOPPED",
            "sink": "OWNED" if self._writer is not None else "NOT_ATTACHED",
            "io_inflight": str(self._writer.io_inflight if self._writer is not None else False),
            "target": "NOT_PROBED",
        }

    @property
    def metrics(self) -> dict[str, float]:
        totals = dict(self._retained)
        engine = self._engine
        if engine is not None:
            for name, value in (
                ("received_total", engine.received_total),
                ("dropped_total", engine.dropped_total),
                ("delivered_total", engine.delivered_total),
                ("failed_total", engine.failed_total),
                ("write_success_total", engine.write_success_total),
            ):
                totals[name] = totals.get(name, 0.0) + float(value)
        totals["io_inflight"] = float(
            self._writer.io_inflight if self._writer is not None else False
        )
        return totals

    @property
    def latency_histograms(self) -> tuple[HistogramSnapshot, ...]:
        active = self._engine
        snapshots = [self._retained_latency.snapshot()]
        if active is not None:
            snapshots.append(active.latency_window.snapshot())
        return tuple(snapshots)

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
                self._snapshot,
                "deliveries",
                instance["template_id"],
                instance["template_version"],
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
                metrics_window_ms=self._snapshot.service["metrics"]["metrics_window_ms"],
                preserve_upstream_sample=(
                    _template(
                        self._snapshot,
                        "sinks",
                        self._snapshot.sinks[self._sink_id]["template_id"],
                        self._snapshot.sinks[self._sink_id]["template_version"],
                    )["kind"]
                    == "kafka"
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
            ("received_total", engine.received_total),
            ("dropped_total", engine.dropped_total),
            ("delivered_total", engine.delivered_total),
            ("failed_total", engine.failed_total),
            ("write_success_total", engine.write_success_total),
        ):
            self._retained[name] = self._retained.get(name, 0.0) + float(value)
        self._retained_latency.absorb(engine.latency_window)
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
        self,
        definition: DeliveryDefinition,
        snapshot: ValidatedSnapshot,
    ) -> RealtimeDeliveryPort:
        if definition.channel != "realtime":
            raise ConfigurationError("RealtimeFactory cannot own reliable Delivery")
        if self._ingress is None:
            nats_config = snapshot.service["nats"]
            self._ingress = NatsRealtimeIngress(
                servers=list(nats_config["servers"]),
                subject_prefix=nats_config["realtime_subject"],
                max_samples=int(nats_config["max_samples_per_batch"]),
                max_bytes=int(nats_config["max_batch_bytes"]),
            )
        return RealtimeDeliveryPort(definition, snapshot, self._pool, self._ingress)
