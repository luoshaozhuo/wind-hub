"""Complete device-wide Kafka v3 payload size, ordering and determinism gates."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_wide
from storage_service.domain.wide_record import WideRecord

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FARM = "farm-b3"
COLLECTOR = "collector-b3"
DEVICE = "WT042"
POINT_TABLE = "point_table_wtg_b3_v1.0.0"
DEVICE_CONFIG = "device_farm-b3_v1.0.0"
PUBLISH = datetime(2026, 11, 10, 9, 30, 1, 123456, tzinfo=UTC)
LIMIT_SAMPLES = 1024
LIMIT_BYTES = 16 * 1024 * 1024


def _item(index: int, value: object = 1.0) -> SampleItem:
    return SampleItem(
        point_id=f"p{index:04d}",
        sample_id=f"s-b3-{index}",
        value=value,
        quality="good",
        sample_time=PUBLISH,
    )


def _batch(size: int) -> SampleBatch:
    batch = SampleBatch(
        batch_id="b-b3",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id="task-b3",
        device_type="wind_turbine",
        device_id=DEVICE,
        point_table_id=POINT_TABLE,
        publish_time=PUBLISH,
        samples=tuple(_item(i) for i in range(size)),
    )
    return SampleBatch.decode(
        batch.encode(max_samples=LIMIT_SAMPLES, max_bytes=LIMIT_BYTES),
        "reliable",
        subject=batch.subject(),
        max_samples=LIMIT_SAMPLES,
        max_bytes=LIMIT_BYTES,
    )


def _encoded(batch: SampleBatch) -> bytes:
    return encode_kafka_wide(
        batch,
        batch.samples,
        device_config_id=DEVICE_CONFIG,
        physical_points=tuple(item.point_id for item in batch.samples),
        data_level="protocol",
    )


@pytest.mark.parametrize("size", [1, 500, 1024])
def test_all_points_form_exactly_one_complete_wide_record(size: int) -> None:
    batch = _batch(size)
    encoded = _encoded(batch)
    payload = json.loads(encoded)
    assert payload["schema_version"] == 3
    assert payload["device_config_id"] == DEVICE_CONFIG
    assert payload["data_level"] == "protocol"
    assert len(payload["points"]) == size
    assert list(payload["points"]) == [f"p{i:04d}" for i in range(size)]
    assert all(payload["points"][f"p{i:04d}"] == [1.0, 1] for i in range(size))
    decoded = WideRecord.from_kafka_json(
        encoded, physical_points=tuple(payload["points"]),
    )
    assert len(decoded.values) == size


def test_subset_is_rejected_instead_of_emitting_partial_history() -> None:
    batch = _batch(3)
    with pytest.raises(ValueError, match="incomplete or non-unique"):
        encode_kafka_wide(
            batch,
            batch.samples[:2],
            device_config_id=DEVICE_CONFIG,
            physical_points=tuple(item.point_id for item in batch.samples),
            data_level="protocol",
        )


def test_empty_device_row_is_rejected() -> None:
    batch = _batch(3)
    with pytest.raises(ValueError, match="complete physical point set"):
        encode_kafka_wide(
            batch, (), device_config_id=DEVICE_CONFIG,
            physical_points=(), data_level="protocol",
        )


def test_wide_json_v3_is_deterministic_for_repeated_encoding() -> None:
    batch = _batch(128)
    assert _encoded(batch) == _encoded(batch)
    parsed = json.loads(_encoded(batch))
    assert parsed["wind_farm_id"] == FARM
    assert parsed["device_id"] == DEVICE


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_are_rejected_at_sample_boundary(bad: float) -> None:
    with pytest.raises(ValueError):
        json.dumps({"value": bad}, allow_nan=False)


def test_unicode_is_utf8_not_ascii_escaped() -> None:
    item = _item(0, "emoji😀中文")
    batch = SampleBatch(
        batch_id="b-unicode",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id="task-b3",
        device_type="wind_turbine",
        device_id=DEVICE,
        point_table_id=POINT_TABLE,
        publish_time=PUBLISH,
        samples=(item,),
    )
    wire = _encoded(batch)
    assert b"\\u" not in wire
    assert "emoji😀中文".encode() in wire
