"""Storage CLI forwards one quality selector inside each grouped v2 filter."""

from unittest.mock import Mock

import pytest

from cli import storage_commands


def test_grouped_query_quality_is_inside_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Mock()
    monkeypatch.setattr(storage_commands, "_invoke", receiver)
    storage_commands.query_samples(
        "farm",
        "2026-11-25T00:00:00Z",
        "2026-11-26T00:00:00Z",
        level="protocol",
        device_ids=["wtg001"],
        point_table_id="point_table_wtg_v1.0.0",
        point_ids=["wind_speed"],
        qualities=[1, 2],
        page_size=20,
        page_token="",
    )
    method, fields = receiver.call_args.args
    assert method == "QuerySamples"
    assert fields["filter"]["qualities"] == [1, 2]
    assert fields["filter"]["device_ids"] == ["wtg001"]
    assert fields["filter"]["point_table_id"] == "point_table_wtg_v1.0.0"
    assert "qualities" not in fields
    assert "wide_filter" not in fields


def test_grouped_aggregate_quality_is_nested_only(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Mock()
    monkeypatch.setattr(storage_commands, "_invoke", receiver)
    storage_commands.query_aggregates(
        "farm",
        "2026-11-25T00:00:00Z",
        "2026-11-26T00:00:00Z",
        level="protocol",
        operation="count",
        window_ms=60_000,
        device_ids=["wtg001"],
        point_table_id="point_table_wtg_v1.0.0",
        point_ids=["wind_speed"],
        qualities=[0, 2],
        page_size=50,
        page_token="",
    )
    method, fields = receiver.call_args.args
    assert method == "QueryAggregates"
    assert fields["filter"]["qualities"] == [0, 2]
    assert fields["aggregation"] == "STORAGE_AGGREGATION_COUNT"
    assert "qualities" not in fields
    assert "wide_filter" not in fields


def test_grouped_export_quality_is_nested_only(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Mock()
    monkeypatch.setattr(storage_commands, "_invoke", receiver)
    storage_commands.create_export(
        "export-1",
        "farm",
        "2026-11-25T00:00:00Z",
        "2026-11-26T00:00:00Z",
        level="business",
        export_format="json",
        device_ids=["wtg001"],
        point_table_id="point_table_wtg_v1.0.0",
        point_ids=["active_power"],
        qualities=[1],
    )
    method, fields = receiver.call_args.args
    assert method == "CreateExport"
    assert fields["filter"]["qualities"] == [1]
    assert fields["request_id"] == "export-1"
    assert fields["export_format"] == "json"
    assert "qualities" not in fields
    assert "wide_filter" not in fields
