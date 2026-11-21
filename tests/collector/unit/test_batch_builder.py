"""Direct boundary tests for the Application-owned Collector BatchBuilder."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from collector.application.batch_builder import BatchBuilder
from core.application import ProtocolSample


def _builder(
    delivery: Literal["realtime", "reliable"] = "reliable", **overrides: int
) -> BatchBuilder:
    options = {
        "max_staleness_ms": 500,
        "max_samples_per_batch": 2,
        "max_batch_bytes": 1024 * 1024,
    }
    options.update(overrides)
    return BatchBuilder(site_id="farm-a", collector_id="collector-1", delivery=delivery, **options)


def _sample(point_id: str, value: object = 1.0) -> ProtocolSample:
    return ProtocolSample(point_id=point_id, value=value, timestamp=datetime.now(UTC))


DEVICE_TYPE = "wind_turbine"
POINT_TABLE = "point_table_modbus_wtg_sim_v1.0.0"


def test_split_order_unique_identities_and_update_site() -> None:
    builder = _builder()
    batches = builder.pack(
        "wtg-01",
        tuple(_sample(f"p{i}") for i in range(5)),
        device_type=DEVICE_TYPE,
        point_table_id=POINT_TABLE,
    )
    assert [len(batch.samples) for batch in batches] == [2, 2, 1]
    assert [item.point_id for batch in batches for item in batch.samples] == [
        f"p{i}" for i in range(5)
    ]
    sample_ids = [item.sample_id for batch in batches for item in batch.samples]
    assert len(set(sample_ids)) == 5
    assert len({batch.batch_id for batch in batches}) == 3
    original = batches[0]
    builder.update_site("farm-b")
    following = builder.pack(
        "wtg-01",
        (_sample("next"),),
        device_type=DEVICE_TYPE,
        point_table_id=POINT_TABLE,
    )[0]
    assert original.wind_farm_id == "farm-a"
    assert following.wind_farm_id == "farm-b"


def test_empty_duplicate_and_oversized_single_sample_are_bounded() -> None:
    builder = _builder(max_samples_per_batch=100, max_batch_bytes=256)
    assert builder.pack("wtg-01", (), device_type=DEVICE_TYPE, point_table_id=POINT_TABLE) == []
    with pytest.raises(ValueError, match="duplicate point_id"):
        builder.pack(
            "wtg-01",
            (_sample("same"), _sample("same")),
            device_type=DEVICE_TYPE,
            point_table_id=POINT_TABLE,
        )
    with pytest.raises(ValueError):
        builder.pack(
            "wtg-01",
            (_sample("small"), _sample("large", "x" * 1024)),
            device_type=DEVICE_TYPE,
            point_table_id=POINT_TABLE,
        )


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_sample_limit_is_rejected(limit: int) -> None:
    with pytest.raises(ValueError, match="batch limits must be positive"):
        _builder(max_samples_per_batch=limit)


def test_nonpositive_byte_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="batch limits must be positive"):
        _builder(max_batch_bytes=0)


def test_realtime_staleness_and_reliable_identity_fields() -> None:
    realtime = _builder("realtime").pack(
        "wtg-01",
        (_sample("p"),),
        device_type=DEVICE_TYPE,
        point_table_id=POINT_TABLE,
    )[0]
    reliable = _builder("reliable").pack(
        "wtg-01",
        (_sample("p"),),
        device_type=DEVICE_TYPE,
        point_table_id=POINT_TABLE,
    )[0]
    assert realtime.samples[0].max_staleness_ms == 500
    assert reliable.samples[0].max_staleness_ms is None
    assert realtime.point_table_id == POINT_TABLE and realtime.device_type == DEVICE_TYPE
