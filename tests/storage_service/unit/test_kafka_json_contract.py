"""T-15/T-44: Kafka JSON v2 wire contract and legacy value rejection."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from storage_service.domain.sample import DataLevel, StorageSample

_BASE_US = 1_800_000_000_000_000


def _rfc3339(microseconds: int) -> str:
    return (
        datetime.fromtimestamp(microseconds / 1_000_000, UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _payload(**overrides: object) -> bytes:
    data: dict[str, object] = {
        "schema_version": 2,
        "wind_farm_id": "farm-f6",
        "collector_id": "collector-f6",
        "task_id": "task-f6",
        "batch_id": "batch-f6",
        "device_type": "WTG",
        "device_id": "WTG-f6",
        "point_table_id": "pt-f6-v1",
        "point_id": "point.f6",
        "sample_id": "sample-f6-0001",
        "data_level": "raw",
        "quality": "good",
        "unit": "",
        "value": 42,
        "sample_time": _rfc3339(_BASE_US),
        "publish_time": _rfc3339(_BASE_US + 10),
        "receive_time": _rfc3339(_BASE_US + 20),
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def test_v2_record_is_accepted_for_every_authoritative_level() -> None:
    assert StorageSample.from_kafka_json(_payload()).data_level == DataLevel.RAW
    protocol = StorageSample.from_kafka_json(_payload(data_level="protocol"))
    assert protocol.data_level == DataLevel.PROTOCOL
    business = StorageSample.from_kafka_json(
        _payload(
            data_level="business",
            business_point_id="bp.active_power",
            unit="kW",
        )
    )
    assert business.data_level == DataLevel.BUSINESS


def test_legacy_json_v1_is_strictly_rejected() -> None:
    # No auto-upgrade, silent mapping or compatibility parsing.
    with pytest.raises(ValueError, match="only Kafka JSON v2 is supported"):
        StorageSample.from_kafka_json(_payload(schema_version=1))
    with pytest.raises(ValueError, match="only Kafka JSON v2 is supported"):
        StorageSample.from_kafka_json(_payload(schema_version="2"))
    with pytest.raises(ValueError, match="only Kafka JSON v2 is supported"):
        StorageSample.from_kafka_json(_payload(schema_version=3))


def test_legacy_engineering_level_is_not_an_alias_for_protocol() -> None:
    # T-44: engineering carries converted engineering semantics and must never
    # be remapped to protocol by string substitution.
    with pytest.raises(ValueError, match="data_level"):
        StorageSample.from_kafka_json(_payload(data_level="engineering"))
    with pytest.raises(ValueError, match="data_level"):
        StorageSample.from_kafka_json(_payload(data_level="PROTOCOL"))
    with pytest.raises(ValueError, match="data_level"):
        StorageSample.from_kafka_json(_payload(data_level=""))
