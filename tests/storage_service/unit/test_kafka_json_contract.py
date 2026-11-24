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
        "value": "002aff",
        "sample_time": _rfc3339(_BASE_US),
        "publish_time": _rfc3339(_BASE_US + 10),
        "receive_time": _rfc3339(_BASE_US + 20),
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def test_v2_record_is_accepted_for_every_authoritative_level() -> None:
    raw = StorageSample.from_kafka_json(_payload())
    assert raw.data_level == DataLevel.RAW
    assert raw.value == b"\x00\x2a\xff"
    protocol = StorageSample.from_kafka_json(_payload(data_level="protocol", value=42))
    assert protocol.data_level == DataLevel.PROTOCOL
    business = StorageSample.from_kafka_json(
        _payload(
            data_level="business",
            value=42,
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


@pytest.mark.parametrize(
    "value",
    [42, 1.5, None, True, "", "0", "abc", "AA", "00FF", "gg", "00" * 2049],
)
def test_raw_requires_canonical_original_bytes(value: object) -> None:
    with pytest.raises(ValueError, match="RAW value"):
        StorageSample.from_kafka_json(_payload(value=value))


def test_raw_identity_is_stable_and_separated_from_protocol() -> None:
    raw = StorageSample.from_kafka_json(_payload(value="002aff"))
    replay = StorageSample.from_kafka_json(_payload(value="002aff"))
    protocol = StorageSample.from_kafka_json(_payload(data_level="protocol", value=42))
    assert raw.value_kind.name == "BYTES"
    assert raw.value == bytes.fromhex("002aff")
    assert raw.logical_key == replay.logical_key
    assert raw.content_sha256 == replay.content_sha256
    assert raw.sample_key == replay.sample_key
    assert raw.logical_key != protocol.logical_key
