"""Sink reference export 单元测试。"""

from datetime import UTC, datetime

from wind_hub_collector.application.sink_export import SinkReferenceExporter
from wind_hub_core.config import IEC104SinkAddress, ResolvedSinkPoint, SinkSource
from wind_hub_core.model.point import PointValue, Quality


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


def test_export_applies_sink_scale_and_offset_once() -> None:
    exporter = SinkReferenceExporter([_definition(scale=2.0, offset=1.0)])
    values = exporter.export(
        [PointValue(device_id="wt01", point_id="wind_speed", value=3.0)]
    )
    assert values[0].value == 7.0


def test_one_internal_point_can_export_to_multiple_refs() -> None:
    exporter = SinkReferenceExporter(
        [_definition(ref="a"), _definition(ref="b")]
    )
    values = exporter.export(
        [PointValue(device_id="wt01", point_id="wind_speed", value=1.0)]
    )
    assert [item.ref for item in values] == ["a", "b"]


def test_unmapped_point_is_ignored() -> None:
    exporter = SinkReferenceExporter([_definition()])
    values = exporter.export(
        [PointValue(device_id="wt01", point_id="other", value=1.0)]
    )
    assert values == []
