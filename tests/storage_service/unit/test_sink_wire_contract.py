"""Sink Kafka JSON v3 complete-device encoding matches Storage's wide decoder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from collector.application.batch_builder import BatchBuilder
from contracts.sample_batch import SampleBatch, SampleItem
from core.application.protocol_contract import ProtocolSample
from sink_service.application.business_projection import BusinessValue
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_wide
from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.domain.wide_row import bind_wide_row
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema

_MOMENT = datetime(2026, 11, 22, 1, 2, 3, 456789, tzinfo=UTC)
_DEVICE_VERSION = "device_farm-f6_v1.0.0"
_POINT = "point.active_power"


def _batch() -> SampleBatch:
    item = SampleItem(
        point_id=_POINT,
        sample_id="sample-f6-0001",
        value=12.5,
        quality="good",
        sample_time=_MOMENT,
        receive_time=_MOMENT,
    )
    return SampleBatch(
        batch_id="batch-f6",
        mode="reliable",
        wind_farm_id="farm-f6",
        collector_id="collector-f6",
        task_id="task-f6",
        device_type="WTG",
        device_id="WTG-f6",
        point_table_id="point_table_storage-points_v1.0.0",
        device_config_id=_DEVICE_VERSION,
        sample_time=_MOMENT,
        publish_time=_MOMENT,
        samples=(item,),
    )


def _wire(
    batch: SampleBatch, items: tuple[SampleItem, ...], *,
    level: str, projection: dict[str, BusinessValue] | None = None,
) -> bytes:
    return encode_kafka_wide(
        batch, items,
        device_config_id=_DEVICE_VERSION,
        physical_points=(_POINT if level != "business" else "bp.active_power",),
        data_level=level,  # type: ignore[arg-type]
        business_values=projection,
    )


def _decode(wire: bytes, *, level: WideDataLevel) -> WideRecord:
    point_id = "bp.active_power" if level is WideDataLevel.BUSINESS else _POINT
    row = WideRecord.from_kafka_json(wire, physical_points=(point_id,))
    assert row.data_level is level
    assert row.device_config_id == _DEVICE_VERSION
    assert row.device_id == "WTG-f6"
    assert row.wind_farm_id == "farm-f6"
    return row


def test_protocol_roundtrip_uses_complete_device_row() -> None:
    batch = _batch()
    row = _decode(_wire(batch, batch.samples, level="protocol"), level=WideDataLevel.PROTOCOL)
    assert row.values == (12.5,)
    assert row.sample_time_us == 1_795_309_323_456_789


def test_business_roundtrip_requires_verified_projection() -> None:
    batch = _batch()
    projection = {
        "sample-f6-0001": BusinessValue(
            business_point_id="bp.active_power",
            unit="kW",
            value=25.0,
        )
    }
    row = _decode(
        _wire(batch, batch.samples, level="business", projection=projection),
        level=WideDataLevel.BUSINESS,
    )
    assert row.point_ids == ("bp.active_power",)
    assert row.values == (25.0,)
    with pytest.raises(ValueError, match="BUSINESS requires"):
        _wire(batch, batch.samples, level="business")


def test_raw_roundtrip_preserves_original_bytes() -> None:
    batch = _batch()
    original = b"\x00\x2a\xff\x80"
    sample = batch.samples[0].model_copy(update={"raw_payload_hex": original.hex()})
    row = _decode(_wire(batch, (sample,), level="raw"), level=WideDataLevel.RAW)
    schema = WideSchema.build(
        point_table_id=batch.point_table_id,
        point_table_version="1.0.0",
        data_level=WideDataLevel.RAW,
        points=(PhysicalPoint(_POINT, "VARBINARY(4)"),),
        max_row_bytes=65536,
    )
    bound = bind_wide_row(row, schema)
    assert bound.values == (original,)


def test_missing_raw_source_bytes_cannot_be_fabricated() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="RAW requires"):
        _wire(batch, batch.samples, level="raw")


def test_collector_preserves_driver_payload_through_wide_wire() -> None:
    original = b"\x00\x01\xff\x80"
    sample = ProtocolSample(
        point_id=_POINT,
        value=12.5,
        timestamp=_MOMENT,
        receive_time=_MOMENT,
        raw_payload=original,
    )
    builder = BatchBuilder(
        site_id="farm-f6",
        collector_id="collector-f6",
        delivery="reliable",
        max_staleness_ms=500,
        max_samples_per_batch=500,
        max_batch_bytes=1048576,
    )
    built = builder.pack(
        "WTG-f6", (sample,), device_type="WTG",
        point_table_id="point_table_storage-points_v1.0.0",
        task_id="collector-task-f6",
    )
    assert len(built) == 1
    decoded = SampleBatch.decode(built[0].encode(), "reliable", subject=built[0].subject())
    row = _decode(_wire(decoded, decoded.samples, level="raw"), level=WideDataLevel.RAW)
    assert row.values == (original.hex(),)


def test_old_point_record_and_engineering_level_are_rejected() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="data_level"):
        _wire(batch, batch.samples, level="engineering")
    with pytest.raises(ValueError, match="Kafka JSON v3 envelope mismatch"):
        WideRecord.from_kafka_json(
            b'{"schema_version":2,"wind_farm_id":"farm"}',
            physical_points=(_POINT,),
        )
