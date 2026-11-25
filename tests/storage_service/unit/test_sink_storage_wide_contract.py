"""Sink Kafka v3 device record round trips through Storage's strict decoder."""

from datetime import UTC, datetime

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_wide
from storage_service.domain.quality_bitmap import unpack_quality_bitmap
from storage_service.domain.wide_record import WideRecord


def test_sink_storage_wide_contract() -> None:
    now = datetime(2026, 11, 25, 12, 0, 0, 100000, tzinfo=UTC)
    batch = SampleBatch(
        batch_id="batch_01",
        mode="reliable",
        wind_farm_id="farm_a",
        collector_id="collector_a",
        task_id="task_a",
        device_type="wind",
        device_id="wtg_001",
        point_table_id="point_table_wind_v1.0.0",
        publish_time=now,
        samples=(
            SampleItem(
                point_id="wind_speed",
                sample_id="s1",
                value=8.2,
                quality="good",
                sample_time=now,
            ),
            SampleItem(
                point_id="active_power",
                sample_id="s2",
                value=None,
                quality="bad",
                sample_time=now,
            ),
        ),
    )
    columns = ("active_power", "wind_speed")
    wire = encode_kafka_wide(
        batch,
        batch.samples,
        device_config_id="device_prod-a_v1.0.1",
        physical_points=columns,
        data_level="protocol",
    )
    decoded = WideRecord.from_kafka_json(wire, physical_points=columns)
    assert decoded.values == (None, 8.2)
    assert decoded.point_ids == columns
    assert decoded.sample_time_us == 1795608000100000
    assert tuple(unpack_quality_bitmap(decoded.quality_bitmap, 2)) == (2, 1)
    assert decoded.device_config_id == "device_prod-a_v1.0.1"
