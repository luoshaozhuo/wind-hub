"""Kafka 遥测输出：每测点一条 JSON 消息，等待 Broker 确认。"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from typing import Any

from core.application.protocol_contract import ConnectionHealth
from core.application.sink_contract import ResolvedSink, ResolvedSinkPoint, StreamSinkAddress
from sink_service.application.error import SinkError
from sink_service.application.export import SinkReferenceExporter
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import KafkaSinkConnection


def _utc(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


class KafkaSink:
    """无内部缓冲的 Kafka Producer；write 完成意味着 Broker 已确认。"""

    def __init__(self, config: ResolvedSink) -> None:
        self._config = KafkaSinkConnection.model_validate(dict(config.connection))
        self._producer: Any = None
        self._lock = asyncio.Lock()
        self._error: str | None = "not opened"
        self._task_sources = (
            {task: set(sources) for task, sources in (config.task_sources or {}).items()}
            if isinstance(config, ResolvedSink)
            else {}
        )
        if not isinstance(config, ResolvedSink):
            raise ValueError("Kafka Sink requires resolved output mappings")
        points = [ResolvedSinkPoint.model_validate(dict(raw)) for raw in config.points]
        for point in points:
            if not isinstance(point.address, StreamSinkAddress):
                raise ValueError("Kafka Sink point requires StreamSinkAddress")
        self._exporter = SinkReferenceExporter(points)

    async def open(self) -> None:
        async with self._lock:
            if self._producer is not None:
                return
            try:
                from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]

                producer = AIOKafkaProducer(
                    bootstrap_servers=self._config.bootstrap_servers,
                    client_id=self._config.client_id,
                    acks="all",
                    enable_idempotence=True,
                    request_timeout_ms=int(self._config.timeout * 1000),
                )
                await producer.start()
            except Exception as exc:
                self._error = str(exc)
                raise SinkError(f"Kafka open failed: {exc}") from exc
            self._producer = producer
            self._error = None

    async def write(self, batch: list[PointValue]) -> None:
        if not batch:
            return
        async with self._lock:
            if self._producer is None:
                raise SinkError("Kafka Sink is not opened")
            try:
                for point in self._exporter.export(batch):
                    if isinstance(point.value, float) and not math.isfinite(point.value):
                        raise SinkError(f"non-finite Kafka value: {point.ref}")
                    address = point.definition.address
                    assert isinstance(address, StreamSinkAddress)
                    identity = address.field
                    payload = {
                        "device_id": point.device_id,
                        "point_id": point.point_id,
                        "value": point.value,
                        "quality": point.quality.value,
                        "timestamp": _utc(point.timestamp),
                        "source": point.source_protocol,
                    }
                    await self._producer.send_and_wait(
                        self._config.topic,
                        key=identity.encode("utf-8"),
                        value=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
                            "utf-8"
                        ),
                    )
            except Exception as exc:
                self._error = str(exc)
                raise SinkError(
                    f"Kafka write failed (some points may be delivered): {exc}"
                ) from exc
            self._error = None

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        await self.write(batch)

    async def flush(self) -> None:
        async with self._lock:
            if self._producer is None:
                raise SinkError("Kafka Sink is not opened")
            await self._producer.flush()

    async def health(self) -> ConnectionHealth:
        async with self._lock:
            return ConnectionHealth(
                healthy=self._producer is not None and self._error is None,
                message=self._error,
            )

    async def close(self) -> None:
        async with self._lock:
            producer, self._producer = self._producer, None
            if producer is not None:
                await producer.stop()
            self._error = "closed"
