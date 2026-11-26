"""Storage CLI forwards one quality selector inside each exact wide filter."""

from unittest.mock import Mock

import pytest

from cli import storage_commands


def test_wide_query_quality_is_top_level(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Mock()
    monkeypatch.setattr(storage_commands, "_invoke", receiver)
    storage_commands.query_wide_rows(
        "farm",
        "wtg001",
        "device_farm_v1.0.0",
        "2026-11-25T00:00:00Z",
        "2026-11-26T00:00:00Z",
        level="protocol",
        point_ids=["wind_speed"],
        qualities=[1, 2],
        page_size=20,
        page_token="",
    )
    method, fields = receiver.call_args.args
    assert method == "QueryWideRows"
    assert fields["qualities"] == [1, 2]
    assert "wide_filter" not in fields


def test_wide_aggregate_quality_is_nested_only(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Mock()
    monkeypatch.setattr(storage_commands, "_invoke", receiver)
    storage_commands.query_wide_aggregates(
        "farm",
        "wtg001",
        "device_farm_v1.0.0",
        "2026-11-25T00:00:00Z",
        "2026-11-26T00:00:00Z",
        level="protocol",
        operation="count",
        window_ms=60_000,
        point_ids=["wind_speed"],
        qualities=[0, 2],
        page_size=50,
        page_token="",
    )
    method, fields = receiver.call_args.args
    assert method == "QueryAggregates"
    assert fields["wide_filter"]["qualities"] == [0, 2]
    assert "qualities" not in fields


def test_wide_export_quality_is_nested_only(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = Mock()
    monkeypatch.setattr(storage_commands, "_invoke", receiver)
    storage_commands.create_wide_export(
        "export-1",
        "farm",
        "wtg001",
        "device_farm_v1.0.0",
        "2026-11-25T00:00:00Z",
        "2026-11-26T00:00:00Z",
        level="business",
        export_format="json",
        point_ids=["active_power"],
        qualities=[1],
        page_size=100,
    )
    method, fields = receiver.call_args.args
    assert method == "CreateExport"
    assert fields["wide_filter"]["qualities"] == [1]
    assert "qualities" not in fields
