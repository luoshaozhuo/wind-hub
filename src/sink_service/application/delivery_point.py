"""A validated source point projected into Sink-owned delivery context.

This is an output value object, not a second NATS wire protocol.
Contracts alone decode and validate SampleBatch v2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from contracts.sample_batch import SampleBatch, SampleItem


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DeliveryPoint:
    """Source identity plus one validated measurement for output adapters."""

    wind_farm_id: str
    collector_id: str
    task_id: str
    device_id: str
    point_id: str
    sample_id: str
    value: float | int | bool | str | None
    quality: Literal["good", "bad", "uncertain"]
    sample_time: datetime
    publish_time: datetime
    receive_time: datetime | None
    max_staleness_ms: int | None

    @classmethod
    def from_batch(cls, batch: SampleBatch, item: SampleItem) -> DeliveryPoint:
        """Project an already validated point without another Pydantic validation."""
        return cls(
            wind_farm_id=batch.wind_farm_id,
            collector_id=batch.collector_id,
            task_id=batch.task_id,
            device_id=batch.device_id,
            point_id=item.point_id,
            sample_id=item.sample_id,
            value=item.value,
            quality=item.quality,
            sample_time=item.sample_time,
            publish_time=batch.publish_time,
            receive_time=item.receive_time,
            max_staleness_ms=item.max_staleness_ms,
        )

    def output_record(self) -> dict[str, object]:
        """Build the stable, 13-field Kafka target record; not a NATS envelope."""
        return {
            "schema_version": 1,
            "wind_farm_id": self.wind_farm_id,
            "collector_id": self.collector_id,
            "task_id": self.task_id,
            "device_id": self.device_id,
            "point_id": self.point_id,
            "sample_id": self.sample_id,
            "value": self.value,
            "quality": self.quality,
            "sample_time": _timestamp(self.sample_time),
            "publish_time": _timestamp(self.publish_time),
            "receive_time": _timestamp(self.receive_time),
            "max_staleness_ms": self.max_staleness_ms,
        }

    def encode(self) -> bytes:
        """Serialize a Sink target record with stable JSON bytes."""
        return json.dumps(
            self.output_record(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


def select_delivery_points(
    batch: SampleBatch, point_ids: frozenset[str] | None = None
) -> tuple[DeliveryPoint, ...]:
    """Project a filtered source batch once for non-Kafka targets."""
    return tuple(
        DeliveryPoint.from_batch(batch, item)
        for item in batch.samples
        if point_ids is None or item.point_id in point_ids
    )
