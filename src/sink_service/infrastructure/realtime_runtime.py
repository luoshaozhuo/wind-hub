"""Realtime NATS ingress and per-Sink shared output resource ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from sink_service.application.config import template_document as _template
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime_delivery import RealtimeDeliveryPort
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.output_pool import SharedSinkOutputPool
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
