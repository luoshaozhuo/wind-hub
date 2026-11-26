"""Bounded Collector in-memory metrics and event journal tests."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from collector.application.telemetry import CollectorTelemetry

pytestmark = pytest.mark.unit


def test_cumulative_and_observed_window_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    telemetry = CollectorTelemetry(windows_seconds=(60, 300))
    telemetry.increment("points.good", 3)
    now[0] = 1070
    assert telemetry.metrics()["points.good"] == 3
    assert "points.good" not in telemetry.window_metrics(60)
    assert telemetry.window_metrics(300)["points.good"] == 3
    telemetry.increment("points.bad")
    assert telemetry.window_metrics(60) == {"points.bad": 1}
    with pytest.raises(ValueError, match="unsupported"):
        telemetry.window_metrics(15)


def test_empty_window_omits_unobserved_metrics() -> None:
    telemetry = CollectorTelemetry()
    assert telemetry.window_metrics(60) == {}


def test_event_capacity_and_time_filter() -> None:
    telemetry = CollectorTelemetry(event_limit=2)
    telemetry.event("INFO", "device-a", "STARTED", "first")
    telemetry.event("WARNING", "device-b", "READ_FAILED", "second")
    telemetry.event("ERROR", "device-c", "LINK_LOST", "third")
    events = telemetry.events()
    assert len(events) == 2
    assert events[0].error_code == "READ_FAILED"
    assert len({entry.event_id for entry in events}) == 2
    assert len(telemetry.events(filter_text="device-b")) == 1
    assert telemetry.events(since=datetime.now(UTC) + timedelta(days=1)) == ()


def test_rejects_negative_counter_and_invalid_capacity() -> None:
    telemetry = CollectorTelemetry()
    with pytest.raises(ValueError):
        telemetry.increment("points.bad", -1)
    with pytest.raises(ValueError):
        CollectorTelemetry(event_limit=0)
