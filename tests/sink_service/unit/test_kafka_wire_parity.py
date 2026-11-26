"""Independent Sink Kafka JSON v3 encoding parity with physical wide records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.business_projection import BusinessValue
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_wide
from storage_service.domain.wide_record import WideRecord

pytestmark = [pytest.mark.unit, pytest.mark.contract]

FARM = "farm-a"
DEVICE = "WT001"
POINT_TABLE = "point_table_wtg_sim_v1.0.0"
CONFIG_ID = "device_farm-a_v1.0.0"
NOW = datetime(2026, 11, 9, 8, 30, 1, 123456, tzinfo=UTC)

VALUES: list[int | float | bool | str | None] = [
    0, 1, -17, 2**53, 0.0, 1.5, -3.14e-9, 1e308, True, False, None,
    "中文，标点·测试", 'quote"back\\slash', "tab\there\nnewline\r",
    "emoji😀unicodeé", "{}[]:,", "", " " * 200,
]
TIMES = [
    datetime(2026, 11, 9, 8, 30, tzinfo=UTC),
    datetime(2026, 11, 9, 8, 30, 1, 120000, tzinfo=UTC),
    datetime(2026, 11, 9, 16, 30, 1, tzinfo=timezone(timedelta(hours=8))),
]


def _batch(value: int | float | bool | str | None, sampled: datetime) -> SampleBatch:
    item = SampleItem(
        point_id="p000",
        sample_id="s-parity-0",
        value=value,
        quality="good",
        sample_time=sampled,
    )
    return SampleBatch(
        batch_id="b-parity",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id="collector-1",
        task_id="task-r9",
        device_type="wind_turbine",
        device_id=DEVICE,
        point_table_id=POINT_TABLE,
        device_config_id=CONFIG_ID,
        sample_time=sampled,
        publish_time=NOW,
        samples=(item,),
    )


def _encode(batch: SampleBatch, *, level: str = "protocol",
            projection: dict[str, BusinessValue] | None = None) -> bytes:
    return encode_kafka_wide(
        batch,
        batch.samples,
        device_config_id=CONFIG_ID,
        physical_points=("p000" if level != "business" else "bp.p000",),
        data_level=level,  # type: ignore[arg-type]
        business_values=projection,
    )


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("sample_time", TIMES)
def test_wide_protocol_bytes_match_independent_reference(
    value: int | float | bool | str | None, sample_time: datetime,
) -> None:
    batch = _batch(value, sample_time)
    decoded = SampleBatch.decode(batch.encode(), "reliable", subject=batch.subject())
    wire = _encode(decoded)
    expected = {
        "schema_version": 3,
        "wind_farm_id": FARM,
        "device_config_id": CONFIG_ID,
        "device_id": DEVICE,
        "data_level": "protocol",
        "sample_time": sample_time.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "points": {"p000": [value, 1]},
    }
    reference = json.dumps(
        expected, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8")
    assert wire == reference
    row = WideRecord.from_kafka_json(wire, physical_points=("p000",))
    assert row.values == (value,)


def test_wide_wire_has_only_seven_authoritative_envelope_fields() -> None:
    payload = json.loads(_encode(_batch(1.5, TIMES[0])))
    assert list(payload) == [
        "schema_version", "wind_farm_id", "device_config_id", "device_id",
        "data_level", "sample_time", "points",
    ]
    assert payload["points"] == {"p000": [1.5, 1]}


def test_protocol_rejects_business_projection() -> None:
    batch = _batch(1.0, TIMES[0])
    with pytest.raises(ValueError, match="RAW/PROTOCOL"):
        _encode(batch, projection={"s-parity-0": object()})  # type: ignore[dict-item]


def test_raw_requires_original_bytes() -> None:
    with pytest.raises(ValueError, match="RAW requires"):
        _encode(_batch(1.0, TIMES[0]), level="raw")


def test_business_requires_complete_projection() -> None:
    batch = _batch(1.0, TIMES[0])
    with pytest.raises(ValueError, match="complete historical projection"):
        _encode(batch, level="business")
    with pytest.raises(ValueError, match="complete historical projection"):
        _encode(batch, level="business", projection={"different-id": object()})  # type: ignore[dict-item]


def test_business_projection_uses_business_point_identity() -> None:
    batch = _batch(1.0, TIMES[0])
    projection = {
        "s-parity-0": BusinessValue(
            business_point_id="bp.p000", unit="kilowatt", value=2.5,
        ),
    }
    raw = _encode(batch, level="business", projection=projection)
    record = WideRecord.from_kafka_json(raw, physical_points=("bp.p000",))
    assert record.values == (2.5,)
    assert record.point_ids == ("bp.p000",)


@pytest.mark.parametrize("bad_payload", [b"not-json", b'{"schema_version":3,"points":NaN}'])
def test_invalid_sample_batch_is_rejected_upstream(bad_payload: bytes) -> None:
    with pytest.raises(Exception):  # noqa: BLE001, B017 - strict upstream payload boundary
        SampleBatch.decode(bad_payload, mode="reliable")
