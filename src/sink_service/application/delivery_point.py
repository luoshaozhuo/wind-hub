"""A validated source point projected into Sink-owned delivery context.

This is an output value object, not a second NATS wire protocol.
Contracts alone decode and validate SampleBatch v3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from contracts.sample_batch import SampleBatch, SampleItem

DataLevel = Literal["raw", "protocol", "business"]


@dataclass(frozen=True, slots=True)
class DeliveryPoint:
    """Source identity plus one level-selected measurement for output adapters."""

    data_level: DataLevel
    wind_farm_id: str
    collector_id: str
    task_id: str
    batch_id: str
    device_type: str
    device_id: str
    point_table_id: str
    point_id: str
    business_point_id: str | None
    sample_id: str
    value: float | int | bool | str | None
    unit: str | None
    quality: Literal["good", "bad", "uncertain"]
    sample_time: datetime
    publish_time: datetime
    receive_time: datetime | None
    max_staleness_ms: int | None
    source_item: SampleItem | None = None

    @classmethod
    def from_batch(
        cls, batch: SampleBatch, item: SampleItem, data_level: DataLevel
    ) -> DeliveryPoint:
        """Select one configured level without inventing source metadata."""
        if data_level not in ("raw", "protocol", "business"):
            raise ValueError("data_level must be raw, protocol or business")
        return cls(
            data_level=data_level,
            wind_farm_id=batch.wind_farm_id,
            collector_id=batch.collector_id,
            task_id=batch.task_id,
            batch_id=batch.batch_id,
            device_type=batch.device_type,
            device_id=batch.device_id,
            point_table_id=batch.point_table_id,
            point_id=item.point_id,
            business_point_id=item.business_point_id if data_level == "business" else None,
            sample_id=item.sample_id,
            value=(
                item.raw_value
                if data_level == "raw"
                else item.business_value
                if data_level == "business"
                else item.value
            ),
            unit=(
                None
                if data_level == "raw"
                else item.business_unit
                if data_level == "business"
                else item.unit
            ),
            quality=item.quality,
            sample_time=item.sample_time,
            publish_time=batch.publish_time,
            receive_time=item.receive_time,
            max_staleness_ms=item.max_staleness_ms,
            source_item=item,
        )


def select_delivery_points(
    batch: SampleBatch,
    data_level: DataLevel,
    point_ids: frozenset[str] | None = None,
) -> tuple[DeliveryPoint, ...]:
    """Project a filtered source batch once for non-Kafka targets."""
    return tuple(
        DeliveryPoint.from_batch(batch, item, data_level)
        for item in batch.samples
        if point_ids is None or item.point_id in point_ids
    )
