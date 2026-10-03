"""CollectorTelemetryStore 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wind_hub_collector.application.runtime.telemetry_store import (
    CollectorTelemetryStore,
)
from wind_hub_core.model.point import PointValue


def _value(
    point_id: str,
    value: float,
    *,
    timestamp: datetime,
    device_id: str = "wtg-01",
) -> PointValue:
    return PointValue(
        device_id=device_id,
        point_id=point_id,
        value=value,
        timestamp=timestamp,
    )


def test_observer_updates_latest_and_preserves_snapshot() -> None:
    store = CollectorTelemetryStore(max_samples_per_point=3)
    now = datetime.now(UTC)
    first = _value("wind_speed", 8.0, timestamp=now)
    second = _value("wind_speed", 9.0, timestamp=now + timedelta(seconds=1))

    store.observe_points([first])
    store.observe_points([second])

    latest = store.latest_for_device("wtg-01")
    assert latest["wind_speed"].value == pytest.approx(9.0)
    assert latest["wind_speed"] is not second


def test_trend_is_bounded_per_point() -> None:
    store = CollectorTelemetryStore(max_samples_per_point=2)
    now = datetime.now(UTC)

    store.observe_points(
        [
            _value("power", 1.0, timestamp=now),
            _value("power", 2.0, timestamp=now + timedelta(seconds=1)),
            _value("power", 3.0, timestamp=now + timedelta(seconds=2)),
        ]
    )

    trend = store.trend_for_device("wtg-01", {"power"})
    assert [item.value for item in trend["power"]] == [2.0, 3.0]


def test_trend_applies_since_and_limit() -> None:
    store = CollectorTelemetryStore(max_samples_per_point=10)
    now = datetime.now(UTC)
    store.observe_points(
        [
            _value("power", 1.0, timestamp=now),
            _value("power", 2.0, timestamp=now + timedelta(seconds=1)),
            _value("power", 3.0, timestamp=now + timedelta(seconds=2)),
        ]
    )

    trend = store.trend_for_device(
        "wtg-01",
        {"power"},
        since=now + timedelta(seconds=1),
        limit_per_point=1,
    )

    assert [item.value for item in trend["power"]] == [3.0]


def test_latest_and_trend_are_isolated_by_device() -> None:
    store = CollectorTelemetryStore()
    now = datetime.now(UTC)
    store.observe_points(
        [
            _value("power", 1.0, timestamp=now, device_id="wtg-01"),
            _value("power", 2.0, timestamp=now, device_id="wtg-02"),
        ]
    )

    assert store.latest_for_device("wtg-01")["power"].value == pytest.approx(1.0)
    assert store.latest_for_device("wtg-02")["power"].value == pytest.approx(2.0)
