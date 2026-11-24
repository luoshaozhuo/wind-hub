"""T-59/T-44: Sink Kafka v2 encoder output satisfies Storage v2 decoding.

Producer/consumer wire-contract test using the real Sink encoder and the real
Storage decoder. It does not replace a live Sink→Storage system test: no Kafka
broker or business process is involved. RAW records are accepted only with source bytes preserved in the
SampleBatch; a missing source payload is never synthesized.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from collector.application.batch_builder import BatchBuilder
from contracts.sample_batch import SampleBatch, SampleItem
from core.application.protocol_contract import ProtocolSample
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


def test_raw_roundtrip_preserves_exact_source_bytes() -> None:
    batch = _batch()
    source_bytes = b"\x00\x2a\xff\x80"
    item = batch.samples[0].model_copy(
        update={"raw_payload_hex": source_bytes.hex()}
    )
    wire = encode_kafka_points(batch, (item,), data_level="raw")[0]
    sample = StorageSample.from_kafka_json(wire)
    assert sample.data_level == StorageDataLevel.RAW
    assert sample.value == source_bytes
    assert sample.unit == ""
    assert sample.business_point_id == ""
    assert sample.content_sha256


def test_missing_source_bytes_cannot_be_fabricated() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="RAW requires"):
        encode_kafka_points(batch, batch.samples, data_level="raw")


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


def test_collector_sample_batch_roundtrip_preserves_driver_raw_bytes() -> None:
    original = b"\x00\x01\xff\x80"
    sample = ProtocolSample(
        point_id="point.active_power",
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
        "WTG-f6",
        (sample,),
        device_type="WTG",
        point_table_id="point_table_storage-points_v1.0.0",
        task_id="collector-task-f6",
    )
    assert len(built) == 1
    decoded = SampleBatch.decode(
        built[0].encode(), "reliable", subject=built[0].subject()
    )
    encoded = encode_kafka_points(decoded, decoded.samples, data_level="raw")
    persisted = StorageSample.from_kafka_json(encoded[0])
    assert persisted.value == original
    assert persisted.value_kind.name == "BYTES"
    assert persisted.sample_id == decoded.samples[0].sample_id
    assert persisted.task_id == decoded.task_id == "collector-task-f6"
