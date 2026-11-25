"""Strict Sink Kafka JSON v2 protocol-record serialization.

RAW uses preserved source bytes; PROTOCOL is decoded; BUSINESS needs historical projection.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.business_projection import BusinessValue

DataLevel = Literal["raw", "protocol", "business"]


def _json_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _require_protocol(level: DataLevel) -> None:
    if level not in ("raw", "protocol", "business"):
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
    data_level: DataLevel,
    business_value: BusinessValue | None = None,
) -> dict[str, object]:
    if not device_type or not point_table_id or not batch_id:
        raise ValueError("v3 source identity required")
    if data_level == "raw":
        raw_hex = item.raw_payload_hex
        if raw_hex is None or not 2 <= len(raw_hex) <= 4096 or len(raw_hex) % 2:
            raise ValueError("RAW requires 1..2048 original payload bytes")
        if any(char not in "0123456789abcdef" for char in raw_hex):
            raise ValueError("RAW payload must be canonical lowercase hex")
    else:
        raw_hex = None
    return {
        "schema_version": 2,
        "data_level": data_level,
        "wind_farm_id": wind_farm_id,
        "collector_id": collector_id,
        "task_id": task_id,
        "device_type": device_type,
        "device_id": device_id,
        "point_table_id": point_table_id,
        "batch_id": batch_id,
        "point_id": item.point_id,
        "business_point_id": business_value.business_point_id if business_value else None,
        "sample_id": item.sample_id,
        "value": raw_hex
        if data_level == "raw"
        else (business_value.value if business_value else item.value),
        "unit": business_value.unit if business_value else None,
        "quality": item.quality,
        "sample_time": _json_timestamp(item.sample_time),
        "publish_time": _json_timestamp(publish_time),
        "receive_time": _json_timestamp(item.receive_time),
        "max_staleness_ms": item.max_staleness_ms,
    }


def _encode(record: dict[str, object]) -> bytes:
    return json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )


def encode_kafka_points(
    batch: SampleBatch,
    items: tuple[SampleItem, ...],
    *,
    data_level: DataLevel,
    business_values: Mapping[str, BusinessValue] | None = None,
) -> tuple[bytes, ...]:
    """Encode verified v2 records; missing BUSINESS projection is a hard failure."""
    _require_protocol(data_level)
    if data_level == "business":
        if business_values is None or set(business_values) != {item.sample_id for item in items}:
            raise ValueError("BUSINESS requires complete sample-specific verified projection")
    elif business_values is not None:
        raise ValueError("RAW/PROTOCOL must not carry BUSINESS projection")
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
                data_level=data_level,
                business_value=business_values[item.sample_id] if business_values else None,
            )
        )
        for item in items
    )


_QUALITY_BITS = {"good": 1, "bad": 2, "uncertain": 3}


def encode_kafka_wide_key(wind_farm_id: str, device_id: str) -> bytes:
    """Partition every data level of one device onto the same Kafka key."""
    for identity in (wind_farm_id, device_id):
        if (
            not isinstance(identity, str) or not identity or ":" in identity
            or len(identity.encode("utf-8")) > 256
        ):
            raise ValueError("Kafka device identity is missing or contains a delimiter")
    return f"{wind_farm_id}:{device_id}".encode("utf-8")


def encode_kafka_wide(
    batch: SampleBatch,
    items: tuple[SampleItem, ...],
    *,
    device_config_id: str,
    physical_points: tuple[str, ...],
    data_level: DataLevel,
    business_values: Mapping[str, BusinessValue] | None = None,
) -> bytes:
    """Encode one complete device-wide Kafka JSON v3 record.

    Only an already synchronized device batch may be encoded; this layer never
    joins points across sample times or invents values for absent samples.
    """
    _require_protocol(data_level)
    if not device_config_id:
        raise ValueError("exact Device Instance version identity required")
    encode_kafka_wide_key(batch.wind_farm_id, batch.device_id)
    if not items or not physical_points or len(set(physical_points)) != len(physical_points):
        raise ValueError("complete physical point set required")
    if data_level == "business":
        if business_values is None or set(business_values) != {item.sample_id for item in items}:
            raise ValueError("BUSINESS requires complete historical projection")
        by_point = {
            business_values[item.sample_id].business_point_id: item for item in items
        }
    else:
        if business_values is not None:
            raise ValueError("RAW/PROTOCOL cannot contain BUSINESS projection")
        by_point = {item.point_id: item for item in items}
    if len(by_point) != len(items) or set(by_point) != set(physical_points):
        raise ValueError("incomplete or non-unique device-wide physical point set")
    timestamps = {item.sample_time for item in items}
    if len(timestamps) != 1:
        raise ValueError("device-wide points do not share one logical sample_time")
    points: dict[str, list[object]] = {}
    for point_id in physical_points:
        item = by_point[point_id]
        value: object = item.value
        if data_level == "raw":
            raw_hex = item.raw_payload_hex
            if raw_hex is None:
                raise ValueError("RAW requires original protocol bytes")
            value = raw_hex
        elif data_level == "business":
            assert business_values is not None
            value = business_values[item.sample_id].value
        points[point_id] = [value, _QUALITY_BITS[item.quality]]
    return _encode(
        {
            "schema_version": 3,
            "wind_farm_id": batch.wind_farm_id,
            "device_config_id": device_config_id,
            "device_id": batch.device_id,
            "data_level": data_level,
            "sample_time": _json_timestamp(next(iter(timestamps))),
            "points": points,
        }
    )
