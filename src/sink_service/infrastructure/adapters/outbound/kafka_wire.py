"""Encode validated points with byte-identical Kafka JSON schema v2.

The strict SampleBatch decoder owns validation. A source batch shares its
immutable identity, data level and publish timestamp across all records, so
encode those fields only once per output chunk.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.delivery_point import DataLevel, DeliveryPoint


def _json_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _dump(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _batch_document(batch: SampleBatch, data_level: DataLevel) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "data_level": data_level,
        "wind_farm_id": batch.wind_farm_id,
        "collector_id": batch.collector_id,
        "task_id": batch.task_id,
        "device_type": batch.device_type,
        "device_id": batch.device_id,
        "point_table_id": batch.point_table_id,
        "batch_id": batch.batch_id,
    }


def _point_document(point: DeliveryPoint, publish_time: str | None = None) -> dict[str, Any]:
    return {
        "point_id": point.point_id,
        "business_point_id": point.business_point_id,
        "sample_id": point.sample_id,
        "value": point.value,
        "unit": point.unit,
        "quality": point.quality,
        "sample_time": _json_timestamp(point.sample_time),
        "publish_time": publish_time or _json_timestamp(point.publish_time),
        "receive_time": _json_timestamp(point.receive_time),
        "max_staleness_ms": point.max_staleness_ms,
    }


def encode_kafka_points(
    batch: SampleBatch, items: tuple[SampleItem, ...], data_level: DataLevel
) -> tuple[bytes, ...]:
    """Encode a device-scoped chunk while retaining shared-prefix batching."""
    if not items:
        return ()
    prefix = _dump(_batch_document(batch, data_level))[:-1] + b","
    publish_time = _json_timestamp(batch.publish_time)
    return tuple(
        prefix
        + _dump(_point_document(DeliveryPoint.from_batch(batch, item, data_level), publish_time))[
            1:
        ]
        for item in items
    )


def encode_kafka_point(batch: SampleBatch, item: SampleItem, data_level: DataLevel) -> bytes:
    """Encode one validated point using the same byte-exact batch encoder."""
    return encode_kafka_points(batch, (item,), data_level)[0]


def encode_delivery_point(point: DeliveryPoint) -> bytes:
    """Serialize a projected point using the stable Kafka JSON v2 schema."""
    document = {
        "schema_version": 2,
        "data_level": point.data_level,
        "wind_farm_id": point.wind_farm_id,
        "collector_id": point.collector_id,
        "task_id": point.task_id,
        "device_type": point.device_type,
        "device_id": point.device_id,
        "point_table_id": point.point_table_id,
        "batch_id": point.batch_id,
        **_point_document(point),
    }
    return _dump(document)
