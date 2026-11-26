"""Collector reliable v4 batches preserve full synchronous device cycles."""

from datetime import UTC, datetime, timedelta

import pytest

from collector.application.batch_builder import BatchBuilder
from core.application.protocol_contract import ProtocolSample


def _builder(*, delivery: str = "reliable", max_samples: int = 500) -> BatchBuilder:
    return BatchBuilder(
        site_id="farm_a",
        collector_id="collector_a",
        delivery=delivery,
        max_staleness_ms=500,
        max_samples_per_batch=max_samples,
        max_batch_bytes=1048576,
    )


def _pack(builder: BatchBuilder, samples: tuple[ProtocolSample, ...], now: datetime):
    return builder.pack(
        "wtg_001",
        samples,
        device_type="wind",
        point_table_id="point_table_wind_v1.2.0",
        task_id="task01",
        device_config_id="device_farm-a_v1.6.0",
        sample_time=now,
        expected_point_ids=("wind_speed", "active_power"),
    )


def test_reliable_full_cycle_preserves_original_timestamp_and_missing_points() -> None:
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    source_time = now - timedelta(milliseconds=20)
    sample = ProtocolSample(
        point_id="wind_speed",
        value=8.2,
        timestamp=source_time,
        raw_payload=b"\x01\x02",
    )
    batches = _pack(_builder(), (sample,), now)
    assert len(batches) == 1
    batch = batches[0]
    assert batch.schema_version == 4
    assert batch.device_config_id == "device_farm-a_v1.6.0"
    assert batch.sample_time == now
    assert [point.point_id for point in batch.samples] == ["wind_speed", "active_power"]
    assert batch.samples[0].source_time == source_time
    assert batch.samples[0].raw_payload_hex == "0102"
    assert batch.samples[1].quality == "not_collected"
    assert batch.samples[1].value is None
    assert batch.samples[1].raw_payload_hex is None
    assert batch.decode(batch.encode(), mode="reliable") == batch


def test_empty_reliable_cycle_is_complete_not_collected_batch() -> None:
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    batches = _pack(_builder(), (), now)
    assert len(batches) == 1
    assert all(point.quality == "not_collected" for point in batches[0].samples)


def test_reliable_device_cycle_never_splits() -> None:
    now = datetime(2026, 11, 25, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="max_samples"):
        _pack(_builder(max_samples=1), (), now)
