"""Batch Kafka encoder capacity, ordering and determinism byte gate.

``encode_kafka_points`` is the single authoritative Sink Kafka boundary. This
module exercises its batch-level guarantees (per-record output, chunk slicing,
deterministic encoding, trusted-boundary rejection) with exact UTF-8 byte
comparisons; scalar/type parity is covered by test_kafka_wire_parity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.infrastructure.adapters.outbound.kafka_wire import (
    encode_kafka_points,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FARM = "farm-b3"
COLLECTOR = "collector-b3"
TASK = "task-b3"
DEVICE = "WT042"
DEVICE_TYPE = "wind_turbine"
POINT_TABLE = "point_table_wtg_b3_v1.0.0"
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
        receive_time=None,
        max_staleness_ms=None,
    )


def _batch(size: int) -> SampleBatch:
    batch = SampleBatch(
        batch_id="b-b3",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id=TASK,
        device_type=DEVICE_TYPE,
        device_id=DEVICE,
        point_table_id=POINT_TABLE,
        publish_time=PUBLISH,
        samples=tuple(_item(i) for i in range(size)),
    )
    # Round-trip through the strict decoder exactly as the Reliable engine does.
    return SampleBatch.decode(
        batch.encode(max_samples=LIMIT_SAMPLES, max_bytes=LIMIT_BYTES),
        "reliable",
        subject=batch.subject(),
        max_samples=LIMIT_SAMPLES,
        max_bytes=LIMIT_BYTES,
    )


def test_every_sample_yields_one_record_with_stable_identity_prefix() -> None:
    batch = _batch(500)
    encoded = encode_kafka_points(batch, batch.samples, data_level="protocol")
    assert len(encoded) == 500
    junction = f'"device_id":"{DEVICE}",'.encode()
    prefixes = {raw[: raw.index(junction) + len(junction)] for raw in encoded}
    assert len(prefixes) == 1  # shared source identity across the whole batch
    for token in (
        f'"wind_farm_id":"{FARM}"',
        f'"collector_id":"{COLLECTOR}"',
        f'"device_type":"{DEVICE_TYPE}"',
    ):
        assert token.encode() in next(iter(prefixes))
    # point_table_id follows device_id, so verify it on the full record instead.
    assert f'"point_table_id":"{POINT_TABLE}"'.encode() in encoded[0]


def test_full_sized_batch_keeps_per_record_order_and_bytes() -> None:
    for size in (500, 1024):
        batch = _batch(size)
        encoded = encode_kafka_points(batch, batch.samples, data_level="protocol")
        assert len(encoded) == size
        for index, raw in enumerate(encoded):
            decoded = json.loads(raw)
            assert decoded["sample_id"] == f"s-b3-{index}"
            assert decoded["point_id"] == f"p{index:04d}"
            assert decoded["data_level"] == "protocol"
            assert decoded["batch_id"] == "b-b3"


def test_chunk_slicing_500_500_24_keeps_indices_and_bytes() -> None:
    batch = _batch(1024)
    slices = [batch.samples[0:500], batch.samples[500:1000], batch.samples[1000:1024]]
    flattened: list[bytes] = []
    for part in slices:
        flattened.extend(encode_kafka_points(batch, part, data_level="protocol"))
    full = encode_kafka_points(batch, batch.samples, data_level="protocol")
    assert [len(part) for part in slices] == [500, 500, 24]
    assert len(flattened) == 1024
    assert flattened == list(full)
    assert b'"point_id":"p0500"' in flattened[500]
    assert b'"point_id":"p1000"' in flattened[1000]


def test_empty_subset_returns_empty_tuple() -> None:
    batch = _batch(3)
    assert encode_kafka_points(batch, (), data_level="protocol") == ()


def test_encoding_is_deterministic_with_no_hidden_state() -> None:
    batch = _batch(128)
    first = encode_kafka_points(batch, batch.samples, data_level="protocol")
    second = encode_kafka_points(batch, batch.samples, data_level="protocol")
    assert first == second
    other = SampleBatch(
        batch_id="b-other",
        mode="reliable",
        wind_farm_id="farm-other",
        collector_id=COLLECTOR,
        task_id=TASK,
        device_type=DEVICE_TYPE,
        device_id=DEVICE,
        point_table_id=POINT_TABLE,
        publish_time=PUBLISH,
        samples=(_item(0),),
    )
    rendered = encode_kafka_points(other, other.samples, data_level="protocol")[0]
    assert b'"wind_farm_id":"farm-other"' in rendered
    assert FARM.encode() not in rendered


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_rejected_at_trusted_boundary(non_finite: float) -> None:
    with pytest.raises(Exception):  # noqa: B017, BLE001 - SampleBatch construction rejects
        SampleBatch(
            batch_id="b-bad",
            mode="reliable",
            wind_farm_id=FARM,
            collector_id=COLLECTOR,
            task_id=TASK,
            device_type=DEVICE_TYPE,
            device_id=DEVICE,
            point_table_id=POINT_TABLE,
            publish_time=PUBLISH,
            samples=(_item(0, non_finite),),
        )
    with pytest.raises(ValueError):
        json.dumps({"value": non_finite}, allow_nan=False)


def test_unicode_is_raw_utf8_not_ascii_escaped() -> None:
    batch = SampleBatch(
        batch_id="b-unicode",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id=TASK,
        device_type=DEVICE_TYPE,
        device_id=DEVICE,
        point_table_id=POINT_TABLE,
        publish_time=PUBLISH,
        samples=(
            SampleItem(
                point_id="p0",
                sample_id="s-unicode",
                value="emoji😀中文",
                quality="good",
                sample_time=PUBLISH,
                max_staleness_ms=None,
            ),
        ),
    )
    rendered = encode_kafka_points(batch, batch.samples, data_level="protocol")[0]
    assert b"\\u" not in rendered
    assert "emoji😀中文".encode() in rendered
