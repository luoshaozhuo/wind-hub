"""T-59/T-44: Sink Kafka v2 encoder output satisfies Storage v2 decoding.

Producer/consumer wire-contract test using the real Sink encoder and the real
Storage decoder. It does not replace a live Sink→Storage system test: no Kafka
broker or business process is involved. Mainline Sink intentionally refuses to
fabricate RAW records; Storage's independent RAW acceptance is verified with an
explicitly constructed valid v2 record instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.business_projection import BusinessValue
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_points
from storage_service.domain.sample import DataLevel as StorageDataLevel
from storage_service.domain.sample import StorageSample

_MOMENT = datetime(2026, 11, 22, 1, 2, 3, 456789, tzinfo=UTC)


def _batch() -> SampleBatch:
    item = SampleItem(
        point_id="point.active_power",
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
        publish_time=_MOMENT,
        samples=(item,),
    )


def test_protocol_record_encodes_and_decodes_across_boundary() -> None:
    batch = _batch()
    wire = encode_kafka_points(batch, batch.samples, data_level="protocol")[0]
    sample = StorageSample.from_kafka_json(wire)
    assert sample.data_level == StorageDataLevel.PROTOCOL
    assert sample.wind_farm_id == "farm-f6"
    assert sample.point_table_id == "point_table_storage-points_v1.0.0"
    assert sample.sample_id == "sample-f6-0001"
    assert sample.business_point_id == ""
    assert sample.unit == ""


def test_business_record_requires_projection_and_decodes() -> None:
    batch = _batch()
    projection = {
        "sample-f6-0001": BusinessValue(
            business_point_id="bp.active_power",
            unit="kW",
            value=12.5,
        )
    }
    wire = encode_kafka_points(
        batch,
        batch.samples,
        data_level="business",
        business_values=projection,
    )[0]
    sample = StorageSample.from_kafka_json(wire)
    assert sample.data_level == StorageDataLevel.BUSINESS
    assert sample.business_point_id == "bp.active_power"
    assert sample.unit == "kW"


def test_business_encoding_without_complete_projection_is_rejected() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="BUSINESS requires"):
        encode_kafka_points(batch, batch.samples, data_level="business")


def test_sink_refuses_to_fabricate_raw_while_storage_accepts_valid_raw() -> None:
    batch = _batch()
    # Mainline policy: Sink cannot produce undecoded RAW frames.
    with pytest.raises(ValueError, match="RAW requires"):
        encode_kafka_points(batch, batch.samples, data_level="raw")  # type: ignore[arg-type]
    # Storage itself still accepts a valid RAW v2 record carrying no unit.
    raw_record = (
        b'{"schema_version":2,"data_level":"raw","wind_farm_id":"farm-f6",'
        b'"collector_id":"collector-f6","task_id":"task-f6","batch_id":"batch-f6",'
        b'"device_type":"WTG","device_id":"WTG-f6",'
        b'"point_table_id":"point_table_storage-points_v1.0.0",'
        b'"point_id":"point.frame","sample_id":"sample-raw-1","quality":"good",'
        b'"value":1,"sample_time":"2026-11-22T01:02:03.456789Z",'
        b'"publish_time":"2026-11-22T01:02:03.456789Z"}'
    )
    sample = StorageSample.from_kafka_json(raw_record)
    assert sample.data_level == StorageDataLevel.RAW
    assert sample.unit == ""


def test_engineering_is_not_an_alias_for_protocol_at_either_boundary() -> None:
    with pytest.raises(ValueError, match="data_level"):
        StorageSample.from_kafka_json(
            b'{"schema_version":2,"data_level":"engineering",'
            b'"wind_farm_id":"f","collector_id":"c","task_id":"t","batch_id":"b",'
            b'"device_type":"d","device_id":"x",'
            b'"point_table_id":"point_table_p_v1.0.0","point_id":"p",'
            b'"sample_id":"s","quality":"good","value":1,'
            b'"sample_time":"2026-11-22T01:02:03.456789Z",'
            b'"publish_time":"2026-11-22T01:02:03.456789Z"}'
        )
    # The Sink encoder rejects any level outside the authoritative set.
    with pytest.raises(ValueError, match="data_level"):
        encode_kafka_points(
            _batch(),
            _batch().samples,
            data_level="engineering",  # type: ignore[arg-type]
        )
