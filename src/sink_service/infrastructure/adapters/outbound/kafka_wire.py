"""Strict Sink Kafka JSON v2 protocol-record serialization.

Protocol is an already protocol-decoded scalar without engineering or business
conversion. Raw frame bytes are not available in SampleBatch v3; this adapter
must not fabricate them. Business records require a verified historical mapping
and cannot be encoded from unprojected samples.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.delivery_point import DeliveryPoint

DataLevel = Literal["raw", "protocol", "business"]


def _json_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _require_protocol(level: DataLevel) -> None:
    if level == "raw":
        raise ValueError("RAW requires original undecoded protocol payload, unavailable")
    if level == "business":
        raise ValueError("BUSINESS requires verified immutable PointTable/Units projection")
    if level != "protocol":
        raise ValueError("data_level must be raw, protocol or business")


def _record(
    *,
    wind_farm_id: str,
    collector_id: str,
    task_id: str,
    device_type: str,
    device_id: str,
    point_table_id: str,
    batch_id: str,
    item: SampleItem,
    publish_time: datetime,
) -> dict[str, object]:
    if not device_type or not point_table_id or not batch_id:
        raise ValueError("v3 source identity required")
    return {
        "schema_version": 2,
        "data_level": "protocol",
        "wind_farm_id": wind_farm_id,
        "collector_id": collector_id,
        "task_id": task_id,
        "device_type": device_type,
        "device_id": device_id,
        "point_table_id": point_table_id,
        "batch_id": batch_id,
        "point_id": item.point_id,
        "business_point_id": None,
        "sample_id": item.sample_id,
        "value": item.value,
        "unit": None,
        "quality": item.quality,
        "sample_time": _json_timestamp(item.sample_time),
        "publish_time": _json_timestamp(publish_time),
        "receive_time": _json_timestamp(item.receive_time),
        "max_staleness_ms": item.max_staleness_ms,
    }


def _encode(record: dict[str, object]) -> bytes:
    return json.dumps(
        record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def encode_kafka_points(
    batch: SampleBatch, items: tuple[SampleItem, ...], *, data_level: DataLevel
) -> tuple[bytes, ...]:
    """Encode one Kafka v2 record per source item, preserving per-item ACK identity."""
    _require_protocol(data_level)
    return tuple(
        _encode(
            _record(
                wind_farm_id=batch.wind_farm_id,
                collector_id=batch.collector_id,
                task_id=batch.task_id,
                device_type=batch.device_type,
                device_id=batch.device_id,
                point_table_id=batch.point_table_id,
                batch_id=batch.batch_id,
                item=item,
                publish_time=batch.publish_time,
            )
        )
        for item in items
    )


def encode_kafka_point(
    batch: SampleBatch, item: SampleItem, *, data_level: DataLevel
) -> bytes:
    """Single-record wrapper for the same strict Kafka v2 schema."""
    return encode_kafka_points(batch, (item,), data_level=data_level)[0]


def encode_delivery_point(point: DeliveryPoint, *, data_level: DataLevel) -> bytes:
    """Encode a validated DeliveryPoint with the same Kafka v2 schema."""
    _require_protocol(data_level)
    item = SampleItem(
        point_id=point.point_id,
        sample_id=point.sample_id,
        value=point.value,
        quality=point.quality,
        sample_time=point.sample_time,
        receive_time=point.receive_time,
        max_staleness_ms=point.max_staleness_ms,
    )
    return _encode(
        _record(
            wind_farm_id=point.wind_farm_id,
            collector_id=point.collector_id,
            task_id=point.task_id,
            device_type=point.device_type,
            device_id=point.device_id,
            point_table_id=point.point_table_id,
            batch_id=point.batch_id,
            item=item,
            publish_time=point.publish_time,
        )
    )
