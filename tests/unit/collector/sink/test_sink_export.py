"""新 Collector Sink reference export 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from collector.application.sink_export import SinkReferenceExporter
from collector.domain.point_value import PointValue
from core.application import Quality
from core.application.sink_config import (
    IEC104SinkAddress,
    ResolvedSinkPoint,
    SinkSource,
)


def _definition(
    *,
    ref: str = "scada.wind_speed",
    scale: float = 1.0,
    offset: float = 0.0,
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id="wind_speed"),
        ref=ref,
        source_data_type="float32",
        source_unit="meter_per_second",
        datatype="float32",
        unit="meter_per_second",
        scale=scale,
        offset=offset,
        address=IEC104SinkAddress(ioa=40101, type_id="M_ME_NC_1"),
    )


def test_export_maps_internal_identity_to_external_ref() -> None:
    exporter = SinkReferenceExporter([_definition()])
    timestamp = datetime(2026, 10, 3, tzinfo=UTC)
    values = exporter.export(
        [
            PointValue(
                device_id="wt01",
                point_id="wind_speed",
                value=8.5,
                quality=Quality.GOOD,
                timestamp=timestamp,
                source="modbus",
            )
        ]
    )
    assert len(values) == 1
    assert values[0].ref == "scada.wind_speed"
    assert values[0].device_id == "wt01"
    assert values[0].point_id == "wind_speed"
    assert values[0].value == 8.5
    assert values[0].quality is Quality.GOOD
    assert values[0].timestamp == timestamp
    assert values[0].source_protocol == "modbus"


def test_export_applies_scale_and_offset() -> None:
    exporter = SinkReferenceExporter([_definition(scale=2.0, offset=1.0)])
    values = exporter.export([PointValue(device_id="wt01", point_id="wind_speed", value=3.0)])
    assert values[0].value == pytest.approx(7.0)


def test_export_ignores_unmapped_points() -> None:
    exporter = SinkReferenceExporter([_definition()])
    values = exporter.export([PointValue(device_id="wt01", point_id="other", value=3.0)])
    assert values == []


def test_export_one_point_to_multiple_refs() -> None:
    exporter = SinkReferenceExporter([_definition(ref="a.x"), _definition(ref="b.x", scale=10.0)])
    values = exporter.export([PointValue(device_id="wt01", point_id="wind_speed", value=2.0)])
    assert {v.ref for v in values} == {"a.x", "b.x"}
    assert sorted(v.value for v in values) == [2.0, 20.0]


def test_export_scale_on_non_numeric_raises_type_error() -> None:
    exporter = SinkReferenceExporter([_definition(scale=2.0)])
    with pytest.raises(TypeError, match="expects numeric value"):
        exporter.export([PointValue(device_id="wt01", point_id="wind_speed", value="on")])
    # bool 同样拒绝（bool 是 int 子类，必须显式排除）
    with pytest.raises(TypeError):
        exporter.export([PointValue(device_id="wt01", point_id="wind_speed", value=True)])


def test_export_none_value_skips_transform() -> None:
    exporter = SinkReferenceExporter([_definition(scale=2.0, offset=1.0)])
    values = exporter.export([PointValue(device_id="wt01", point_id="wind_speed", value=None)])
    assert values[0].value is None
