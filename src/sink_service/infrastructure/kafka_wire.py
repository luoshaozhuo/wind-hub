"""Encode validated Reliable points with byte-identical Kafka JSON schema.

The strict SampleBatch decoder owns validation. A source batch shares its
immutable identity and publish timestamp across all records, so encode those
fields only once per output chunk.
"""

from __future__ import annotations

import json
from datetime import datetime

from contracts.sample_batch import SampleBatch, SampleItem


def _json_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def encode_kafka_points(batch: SampleBatch, items: tuple[SampleItem, ...]) -> tuple[bytes, ...]:
    """Encode a device-scoped chunk, preserving the exact 13-field wire bytes."""
    if not items:
        return ()

    prefix = (
        json.dumps(
            {
                "schema_version": 1,
                "wind_farm_id": batch.wind_farm_id,
                "collector_id": batch.collector_id,
                "task_id": batch.task_id,
                "device_id": batch.device_id,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")[:-1]
        + b","
    )
    publish_time = _json_timestamp(batch.publish_time)

    return tuple(
        prefix
        + json.dumps(
            {
                "point_id": item.point_id,
                "sample_id": item.sample_id,
                "value": item.value,
                "quality": item.quality,
                "sample_time": _json_timestamp(item.sample_time),
                "publish_time": publish_time,
                "receive_time": _json_timestamp(item.receive_time),
                "max_staleness_ms": item.max_staleness_ms,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")[1:]
        for item in items
    )


def encode_kafka_point(batch: SampleBatch, item: SampleItem) -> bytes:
    """Encode one validated point using the same byte-exact batch encoder."""
    return encode_kafka_points(batch, (item,))[0]
