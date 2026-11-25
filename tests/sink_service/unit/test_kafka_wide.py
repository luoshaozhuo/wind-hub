"""Strict synchronized Sink Kafka JSON v3 wide-record encoding."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.business_projection import BusinessValue
from sink_service.infrastructure.adapters.outbound.kafka_wire import (
    encode_kafka_wide,
    encode_kafka_wide_key,
)


def _batch() -> SampleBatch:
    stamp = datetime(2026, 11, 25, 12, tzinfo=UTC)
    return SampleBatch(
        batch_id="b1",
        mode="reliable",
        wind_farm_id="farm_a",
        collector_id="collector_a",
        task_id="task_a",
        device_type="wind_turbine",
        device_id="wtg_001",
        point_table_id="point_table_example_v1.0.0",
        publish_time=stamp,
        samples=(
            SampleItem(
                point_id="wind_speed",
                sample_id="s1",
                value=8.2,
                quality="good",
                sample_time=stamp,
            ),
            SampleItem(
                point_id="active_power",
                sample_id="s2",
                value=None,
                quality="bad",
                sample_time=stamp,
            ),
        ),
    )


def test_one_device_record_with_complete_points() -> None:
    batch = _batch()
    raw = encode_kafka_wide(
        batch,
        batch.samples,
        device_config_id="device_prod-a_v1.0.1",
        physical_points=("active_power", "wind_speed"),
        data_level="protocol",
    )
    data = json.loads(raw)
    assert set(data) == {
        "schema_version", "wind_farm_id", "device_config_id",
        "device_id", "data_level", "sample_time", "points",
    }
    assert data["schema_version"] == 3
    assert list(data["points"]) == ["active_power", "wind_speed"]
    assert data["points"] == {"active_power": [None, 2], "wind_speed": [8.2, 1]}


def test_partial_device_record_is_rejected() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="incomplete"):
        encode_kafka_wide(
            batch, batch.samples[:1],
            device_config_id="device_prod-a_v1.0.1",
            physical_points=("active_power", "wind_speed"),
            data_level="protocol",
        )


def test_mixed_logical_times_are_rejected() -> None:
    batch = _batch()
    delayed = batch.samples[1].model_copy(
        update={"sample_time": batch.samples[1].sample_time + timedelta(milliseconds=1)}
    )
    with pytest.raises(ValueError, match="logical sample_time"):
        encode_kafka_wide(
            batch, (batch.samples[0], delayed),
            device_config_id="device_prod-a_v1.0.1",
            physical_points=("active_power", "wind_speed"),
            data_level="protocol",
        )


def test_raw_requires_original_capture() -> None:
    batch = _batch()
    with pytest.raises(ValueError, match="original protocol bytes"):
        encode_kafka_wide(
            batch, batch.samples,
            device_config_id="device_prod-a_v1.0.1",
            physical_points=("active_power", "wind_speed"),
            data_level="raw",
        )


def test_business_columns_use_standard_ids_and_reject_non_bijective_mapping() -> None:
    batch = _batch()
    projected = {
        "s1": BusinessValue("hub_wind_speed", "m_per_s", 8.2),
        "s2": BusinessValue("grid_active_power", "kw", None),
    }
    raw = encode_kafka_wide(
        batch, batch.samples,
        device_config_id="device_prod-a_v1.0.1",
        physical_points=("grid_active_power", "hub_wind_speed"),
        data_level="business",
        business_values=projected,
    )
    assert set(json.loads(raw)["points"]) == {"hub_wind_speed", "grid_active_power"}
    projected["s2"] = BusinessValue("hub_wind_speed", "kw", None)
    with pytest.raises(ValueError, match="non-unique"):
        encode_kafka_wide(
            batch, batch.samples,
            device_config_id="device_prod-a_v1.0.1",
            physical_points=("grid_active_power", "hub_wind_speed"),
            data_level="business",
            business_values=projected,
        )


def test_kafka_key_uses_only_wind_farm_and_device() -> None:
    assert encode_kafka_wide_key("farm_a", "wtg_001") == b"farm_a:wtg_001"
    with pytest.raises(ValueError, match="delimiter"):
        encode_kafka_wide_key("farm:a", "wtg_001")
