"""Compose NATS consumers and target adapters without importing Collector."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from core.infrastructure.config import YamlConfigAdapter
from core.infrastructure.sink import GrpcSink, KafkaSink, ModbusSink, RedisSink, TDengineSink
from contracts.sample_message import DeliveryMode, SampleMessage
from sink_service.application.runtime import SinkRuntime
from sink_service.infrastructure.config import _resolve_sinks
from sink_service.infrastructure.nats_consumer import NatsSinkConsumer
from sink_service.infrastructure.point_adapter import PointValueSinkAdapter

_TARGETS = {
    "redis": RedisSink,
    "tdengine": TDengineSink,
    "modbus": ModbusSink,
    "grpc": GrpcSink,
    "kafka": KafkaSink,
}


def assemble_sinks(
    config_dir: str | Path,
    *,
    servers: tuple[str, ...],
    delivery: DeliveryMode,
    wind_farm_id: str,
) -> dict[str, SinkRuntime]:
    """Build one consumer per enabled target with independent acknowledgments."""
    snapshot = YamlConfigAdapter(Path(config_dir)).load()
    if snapshot.site.site_id != wind_farm_id:
        raise ValueError("wind_farm_id differs from site configuration")
    configs = _resolve_sinks(snapshot)
    result: dict[str, SinkRuntime] = {}
    for sink_id, definition in configs.items():
        if not definition.enabled:
            continue
        if delivery == "reliable" and definition.kind != "tdengine":
            raise ValueError(
                f"Sink {sink_id}: reliable delivery requires a durable idempotent target"
            )
        target = _TARGETS[definition.kind](definition)
        adapter = PointValueSinkAdapter(target)
        allowed = {
            (task_id, source_device, source_point)
            for task_id, sources in (definition.task_sources or {}).items()
            for source_device, source_point in sources
        }
        if not allowed:
            allowed = {
                (task_id, item["source"]["device_id"], item["source"]["point_id"])
                for task_id, refs in (definition.task_refs or {}).items()
                for item in definition.points
                if (item.get("ref") or
                    f"{item['source']['device_id']}.{item['source']['point_id']}") in refs
            }
        runtime: SinkRuntime
        async def handle(
            sample: SampleMessage,
            *,
            accepted: frozenset[tuple[str, str, str]] = frozenset(allowed),
        ) -> None:
            if (sample.task_id, sample.device_id, sample.point_id) not in accepted:
                return
            await runtime.write(sample)

        subject = f"wh.{'rt' if delivery == 'realtime' else 'hist'}.{wind_farm_id}.>"
        consumer = NatsSinkConsumer(
            servers,
            subject,
            durable=(f"wh_{wind_farm_id}_{sink_id}" if delivery == "reliable" else None),
            handler=handle,
            max_ack_pending=1,
        )
        runtime = SinkRuntime(adapter, consumer, realtime=delivery == "realtime")
        result[sink_id] = runtime
    return result
