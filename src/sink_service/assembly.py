"""Compose NATS consumers and target adapters without importing Collector."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from hashlib import sha256
from pathlib import Path

from core.domain.sink import Sink
from core.infrastructure.config import YamlConfigAdapter
from sink_service.infrastructure.adapters import GrpcSink, KafkaSink, ModbusSink, RedisSink, TDengineSink
from contracts.sample_message import DeliveryMode, SampleMessage, validate_subject_token
from sink_service.application.point_port import LegacyPointSinkPort
from sink_service.application.runtime import SinkRuntime
from sink_service.infrastructure.config import _resolve_sinks
from sink_service.infrastructure.intent import SinkIntentStore
from sink_service.infrastructure.nats_consumer import NatsSinkConsumer
from sink_service.infrastructure.point_adapter import PointValueSinkAdapter

_TARGETS: dict[str, Callable[[Sink], LegacyPointSinkPort]] = {
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
    intent_store: SinkIntentStore | None = None,
    sink_ids: frozenset[str] | None = None,
) -> dict[str, SinkRuntime]:
    """Build one consumer per enabled target with independent acknowledgments."""
    validate_subject_token(wind_farm_id)
    snapshot = YamlConfigAdapter(Path(config_dir)).load()
    if snapshot.site.site_id != wind_farm_id:
        raise ValueError("wind_farm_id differs from site configuration")
    configs = _resolve_sinks(snapshot)
    if sink_ids is not None and not sink_ids.issubset(configs):
        raise ValueError(f"unknown configured Sinks: {sorted(sink_ids - configs.keys())}")
    result: dict[str, SinkRuntime] = {}
    for sink_id, definition in configs.items():
        if not definition.enabled or (sink_ids is not None and sink_id not in sink_ids):
            continue
        if delivery == "reliable" and definition.kind != "tdengine":
            raise ValueError(f"reliable Sink {sink_id} has no durable idempotency contract")
        allowed = frozenset(
            (task_id, device_id, point_id)
            for task_id, sources in (definition.task_sources or {}).items()
            for device_id, point_id in sources
        )
        explicit = frozenset(
            (task_id, mapping["source"]["device_id"], mapping["source"]["point_id"])
            for task_id, output in definition.outputs.items()
            for mapping in output.points
        )
        allowed = allowed | explicit
        if not allowed and definition.kind not in {"redis", "tdengine"}:
            raise ValueError(f"Sink {sink_id} requires explicit source mappings")
        target = _TARGETS[definition.kind](definition)
        result[sink_id] = _build_runtime(
            PointValueSinkAdapter(target),
            servers=servers,
            delivery=delivery,
            subject=f"wh.{'rt' if delivery == 'realtime' else 'hist'}.{wind_farm_id}.>",
            durable=(
                "sink_" + sha256(f"{wind_farm_id}:{sink_id}".encode()).hexdigest()[:32]
                if delivery == "reliable" else None
            ),
            allowed=allowed,
            save_intent=(partial(intent_store.set_running, sink_id) if intent_store else None),
        )
    return result


def _build_runtime(
    adapter: PointValueSinkAdapter,
    *,
    servers: tuple[str, ...],
    delivery: DeliveryMode,
    subject: str,
    durable: str | None,
    allowed: frozenset[tuple[str, str, str]],
    save_intent: Callable[[bool], None] | None,
) -> SinkRuntime:
    """Bind every handler to its own runtime, never a shared closure variable."""
    holder: list[SinkRuntime] = []

    async def handle(sample: SampleMessage) -> None:
        if (sample.task_id, sample.device_id, sample.point_id) not in allowed:
            return
        await holder[0].write(sample)

    consumer = NatsSinkConsumer(
        servers, subject, durable=durable, handler=handle, max_ack_pending=1,
    )
    runtime = SinkRuntime(
        adapter, consumer, realtime=delivery == "realtime", save_intent=save_intent,
    )
    holder.append(runtime)
    return runtime
