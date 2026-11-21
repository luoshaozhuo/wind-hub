"""Kafka JSON v2 record wire parity against an independent reference encoder.

The authoritative Sink Kafka boundary is the batch encoder
``encode_kafka_points``. This module rebuilds the same 18-field record with a
single independent ``json.dumps`` (a different code path than production) and
compares exact UTF-8 bytes. It also pins PROTOCOL/BUSINESS separation and keeps
malformed payload rejection at the trusted SampleBatch boundary.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.infrastructure.adapters.outbound.kafka_wire import (
    encode_kafka_points,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FARM = "farm-a"
COLLECTOR = "collector-1"
TASK = "task-r9"
DEVICE = "WT001"
DEVICE_TYPE = "wind_turbine"
POINT_TABLE = "point_table_wtg_sim_v1.0.0"
NOW = datetime(2026, 11, 9, 8, 30, 1, 123456, tzinfo=UTC)

# Canonical 18-field order of the Kafka JSON v2 record.
WIRE_FIELD_ORDER = [
    "schema_version",
    "data_level",
    "wind_farm_id",
    "collector_id",
    "task_id",
    "device_type",
    "device_id",
    "point_table_id",
    "batch_id",
    "point_id",
    "business_point_id",
    "sample_id",
    "value",
    "unit",
    "quality",
    "sample_time",
    "publish_time",
    "receive_time",
    "max_staleness_ms",
]

SPECIAL_STRINGS = [
    "中文，标点·测试",
    'quote"back\\slash',
    "tab\there\nnewline\r",
    "emoji😀unicodeé",
    "{}[]:,",
    "null true false 1.5e10",
    "",
    " " * 200,
]

VALUES: list = [
    0,
    1,
    -17,
    2**53,
    0.0,
    1.5,
    -3.14e-9,
    1e308,
    True,
    False,
    None,
    *SPECIAL_STRINGS,
]

TIMES = [
    datetime(2026, 11, 9, 8, 30, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, 120000, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, 123456, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, 100, tzinfo=UTC),
    datetime(2026, 11, 9, 16, 30, 1, tzinfo=timezone(timedelta(hours=8))),
]


def _json_ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _batch(value, sample_time: datetime, *, receive_time=None) -> SampleBatch:
    item = SampleItem(
        point_id="p000",
        sample_id="s-parity-0",
        value=value,
        quality="good",
        sample_time=sample_time,
        receive_time=receive_time,
        max_staleness_ms=None,
    )
    return SampleBatch(
        batch_id="b-parity",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id=TASK,
        device_type=DEVICE_TYPE,
        device_id=DEVICE,
        point_table_id=POINT_TABLE,
        publish_time=NOW,
        samples=(item,),
    )


def _reference_record(batch: SampleBatch, item: SampleItem) -> dict[str, object]:
    """Independent single-dict PROTOCOL reference (no engineering conversion)."""
    return {
        "schema_version": 2,
        "data_level": "protocol",
        "wind_farm_id": batch.wind_farm_id,
        "collector_id": batch.collector_id,
        "task_id": batch.task_id,
        "device_type": batch.device_type,
        "device_id": batch.device_id,
        "point_table_id": batch.point_table_id,
        "batch_id": batch.batch_id,
        "point_id": item.point_id,
        "business_point_id": None,
        "sample_id": item.sample_id,
        "value": item.value,
        "unit": None,
        "quality": item.quality,
        "sample_time": _json_ts(item.sample_time),
        "publish_time": _json_ts(batch.publish_time),
        "receive_time": _json_ts(item.receive_time),
        "max_staleness_ms": item.max_staleness_ms,
    }


def _all_pairs():
    return [
        (value, sample_time, receive_time)
        for value in VALUES
        for sample_time in TIMES
        for receive_time in (None, sample_time)
    ]


@pytest.mark.parametrize(("value", "sample_time", "receive_time"), _all_pairs())
def test_protocol_bytes_identical_to_independent_reference(
    value, sample_time, receive_time
) -> None:
    batch = _batch(value, sample_time, receive_time=receive_time)
    decoded = SampleBatch.decode(batch.encode(), "reliable", subject=batch.subject())
    item = decoded.samples[0]

    production_bytes = encode_kafka_points(decoded, (item,), data_level="protocol")[0]
    reference_bytes = json.dumps(
        _reference_record(decoded, item),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert production_bytes == reference_bytes
    assert json.loads(production_bytes) == json.loads(reference_bytes)


def test_reliable_mode_renders_null_optional_fields() -> None:
    batch = _batch(1.0, TIMES[0])
    decoded = SampleBatch.decode(batch.encode(), mode="reliable")
    rendered = encode_kafka_points(decoded, decoded.samples, data_level="protocol")[0].decode()
    assert '"max_staleness_ms":null' in rendered
    assert '"receive_time":null' in rendered
    assert '"business_point_id":null' in rendered
    assert '"unit":null' in rendered
    assert '"data_level":"protocol"' in rendered


def test_field_order_matches_kafka_json_v2_schema() -> None:
    batch = _batch(1.5, TIMES[2], receive_time=TIMES[2])
    decoded = SampleBatch.decode(batch.encode(), mode="reliable")
    rendered = encode_kafka_points(decoded, decoded.samples, data_level="protocol")[0].decode()
    keys = [segment.split(":", 1)[0].strip('"') for segment in rendered.strip("{}").split(",")]
    assert keys == WIRE_FIELD_ORDER


def test_protocol_rejects_business_projection() -> None:
    batch = _batch(1.0, TIMES[0])
    with pytest.raises(ValueError, match="PROTOCOL must not carry BUSINESS"):
        encode_kafka_points(
            batch,
            batch.samples,
            data_level="protocol",
            business_values={"s-parity-0": object()},  # type: ignore[arg-type]
        )


def test_raw_level_is_rejected() -> None:
    batch = _batch(1.0, TIMES[0])
    with pytest.raises(ValueError, match="RAW"):
        encode_kafka_points(batch, batch.samples, data_level="raw")  # type: ignore[arg-type]


def test_business_requires_complete_sample_specific_projection() -> None:
    batch = _batch(1.0, TIMES[0])
    # Missing projection entirely.
    with pytest.raises(ValueError, match="complete sample-specific"):
        encode_kafka_points(batch, batch.samples, data_level="business")
    # Projection keyed to the wrong sample id is also incomplete.
    with pytest.raises(ValueError, match="complete sample-specific"):
        encode_kafka_points(
            batch,
            batch.samples,
            data_level="business",
            business_values={"wrong-id": object()},  # type: ignore[dict-item]
        )


@pytest.mark.parametrize(
    "bad_payload",
    [
        b'{"schema_version":3,"batch_id":"b","mode":"reliable","wind_farm_id":"f",'
        b'"collector_id":"c","task_id":"t","device_type":"wt","device_id":"d",'
        b'"point_table_id":"point_table_x_v1.0.0","publish_time":"2026-11-09T08:30:00Z",'
        b'"samples":[{"point_id":"p","sample_id":"s","value":NaN,'
        b'"quality":"good","sample_time":"2026-11-09T08:30:00Z"}]}',
        b'{"schema_version":3,"batch_id":"b","mode":"reliable","wind_farm_id":"f",'
        b'"collector_id":"c","task_id":"t","device_type":"wt","device_id":"d",'
        b'"point_table_id":"point_table_x_v1.0.0","publish_time":"2026-11-09T08:30:00Z",'
        b'"samples":[{"point_id":"bad\\npoint","sample_id":"s","value":1,'
        b'"quality":"good","sample_time":"2026-11-09T08:30:00Z"}]}',
        b"not-json",
    ],
)
def test_invalid_payloads_still_rejected_at_trusted_boundary(bad_payload: bytes) -> None:
    with pytest.raises(Exception):  # noqa: B017, BLE001 - boundary rejection class is open
        SampleBatch.decode(bad_payload, mode="reliable")
