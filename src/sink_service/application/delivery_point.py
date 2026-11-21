"""A validated source point projected into Sink-owned delivery context.

This is an output value object, not a second NATS wire protocol.
Contracts alone decode and validate SampleBatch v3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from contracts.sample_batch import SampleBatch, SampleItem


@dataclass(frozen=True, slots=True)
class DeliveryPoint:
    """Source identity plus one validated measurement for output adapters."""

    wind_farm_id: str
    collector_id: str
    task_id: str
    device_type: str
    device_id: str
    point_table_id: str
    batch_id: str
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
            device_type=batch.device_type,
            device_id=batch.device_id,
            point_table_id=batch.point_table_id,
            batch_id=batch.batch_id,
            point_id=item.point_id,
            sample_id=item.sample_id,
            value=item.value,
            quality=item.quality,
            sample_time=item.sample_time,
            publish_time=batch.publish_time,
            receive_time=item.receive_time,
            max_staleness_ms=item.max_staleness_ms,
        )


def select_delivery_points(
    batch: SampleBatch, point_ids: frozenset[str] | None = None
) -> tuple[DeliveryPoint, ...]:
    """Project a filtered source batch once for non-Kafka targets."""
    return tuple(
        DeliveryPoint.from_batch(batch, item)
        for item in batch.samples
        if point_ids is None or item.point_id in point_ids
    )
