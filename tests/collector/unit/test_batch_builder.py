"""Direct boundary tests for the Application-owned Collector BatchBuilder."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from collector.application.batch_builder import BatchBuilder
from core.application import ProtocolSample
from core.domain import (
    BusinessPoint,
    DataType,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    Quantity,
    Unit,
)

_UNIT = Unit("kilowatt", "kW", Quantity("active_power"))
_BUSINESS = BusinessPoint("active_power", DataType.FLOAT64, _UNIT)


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


def _pack(builder: BatchBuilder, samples: tuple[ProtocolSample, ...]):
    table = PointTable(
        "modbus_wtg@1.0.0",
        Protocol("modbus"),
        {
            sample.point_id: Point(sample.point_id, "active_power", _UNIT, PointAccess.READ)
            for sample in samples
        },
    )
    return builder.pack(
        "wtg-01",
        samples,
        device_type="turbine",
        point_table_id="point_table_modbus_wtg_v1.0.0",
        point_table=table,
        business_points={"active_power": _BUSINESS},
    )


def test_split_order_unique_identities_and_update_site() -> None:
    builder = _builder()
    batches = _pack(builder, tuple(_sample(f"p{i}") for i in range(5)))
    assert [len(batch.samples) for batch in batches] == [2, 2, 1]
    assert [item.point_id for batch in batches for item in batch.samples] == [
        f"p{i}" for i in range(5)
    ]
    sample_ids = [item.sample_id for batch in batches for item in batch.samples]
    assert len(set(sample_ids)) == 5
    assert len({batch.batch_id for batch in batches}) == 3
    original = batches[0]
    assert original.device_type == "turbine"
    assert original.point_table_id == "point_table_modbus_wtg_v1.0.0"
    assert original.samples[0].raw_value == 1.0
    assert original.samples[0].business_point_id == "active_power"
    builder.update_site("farm-b")
    following = _pack(builder, (_sample("next"),))[0]
    assert original.wind_farm_id == "farm-a"
    assert following.wind_farm_id == "farm-b"


def test_empty_duplicate_and_oversized_single_sample_are_bounded() -> None:
    builder = _builder(max_samples_per_batch=100, max_batch_bytes=256)
    assert _pack(builder, ()) == []
    with pytest.raises(ValueError, match="duplicate point_id"):
        _pack(builder, (_sample("same"), _sample("same")))
    with pytest.raises(ValueError):
        _pack(builder, (_sample("small"), _sample("large", "x" * 1024)))


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_sample_limit_is_rejected(limit: int) -> None:
    with pytest.raises(ValueError, match="batch limits must be positive"):
        _builder(max_samples_per_batch=limit)


def test_nonpositive_byte_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="batch limits must be positive"):
        _builder(max_batch_bytes=0)


def test_protocol_and_business_values_use_distinct_authoritative_transforms() -> None:
    quantity = Quantity("active_power")
    watt = Unit("watt", "W", quantity)
    kilowatt = Unit("kilowatt", "kW", quantity, scale_to_base=1000.0)
    business = BusinessPoint("active_power", DataType.FLOAT64, kilowatt)
    table = PointTable(
        "modbus_wtg@1.0.0",
        Protocol("modbus"),
        {"p": Point("p", "active_power", watt, PointAccess.READ, scale=2.0)},
    )
    batch = _builder().pack(
        "wtg-01",
        (_sample("p", 10.0),),
        device_type="turbine",
        point_table_id="point_table_modbus_wtg_v1.0.0",
        point_table=table,
        business_points={"active_power": business},
    )[0]
    item = batch.samples[0]
    assert item.raw_value == 10.0
    assert item.value == 20.0
    assert item.unit == "watt"
    assert item.business_value == 0.02
    assert item.business_unit == "kilowatt"


def test_realtime_staleness_and_reliable_identity_fields() -> None:
    realtime = _pack(_builder("realtime"), (_sample("p"),))[0]
    reliable = _pack(_builder("reliable"), (_sample("p"),))[0]
    assert realtime.samples[0].max_staleness_ms == 500
    assert reliable.samples[0].max_staleness_ms is None
