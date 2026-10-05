"""Server Prometheus 快照 Gauge 单元测试。"""

from __future__ import annotations

from prometheus_client import REGISTRY

from wind_hub_server.application.port.monitoring import (
    CounterSnapshot,
    RuntimeStatusSnapshot,
)
from wind_hub_server.infra import metrics


def test_update_runtime_gauges_sets_cached_collector_values() -> None:
    metrics.update_runtime_gauges(
        RuntimeStatusSnapshot(
            device_count=3,
            devices_connected=2,
            sink_count=4,
            sinks_healthy=1,
            points_collected=100,
            points_routed=90,
            points_dropped=10,
        ),
        CounterSnapshot(
            points_bad=2,
            acquisition_runs=8,
            acquisition_failures=1,
            acquisition_partial=1,
            missed_cycles=3,
            poll_overruns=2,
            connect_failures=4,
            reconnects=5,
        ),
    )

    assert REGISTRY.get_sample_value("wind_hub_devices_total") == 3.0
    assert REGISTRY.get_sample_value("wind_hub_devices_connected") == 2.0
    assert REGISTRY.get_sample_value("wind_hub_sinks_total") == 4.0
    assert REGISTRY.get_sample_value("wind_hub_sinks_healthy") == 1.0
    assert REGISTRY.get_sample_value("wind_hub_points_collected_total") == 100.0
    assert REGISTRY.get_sample_value("wind_hub_points_routed_total") == 90.0
    assert REGISTRY.get_sample_value("wind_hub_points_dropped_total") == 10.0
    assert REGISTRY.get_sample_value("wind_hub_points_bad_total") == 2.0
    assert REGISTRY.get_sample_value("wind_hub_acquisition_runs_total") == 8.0
    assert REGISTRY.get_sample_value("wind_hub_device_reconnect_total") == 5.0


def test_update_device_gauges_sets_and_prunes_stale_series() -> None:
    metrics.update_device_gauges(
        [("metrics-dev-a", "modbus", True), ("metrics-dev-b", "ads", False)]
    )
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_device_connected",
            {"device_id": "metrics-dev-a", "protocol": "modbus"},
        )
        == 1.0
    )
    metrics.update_device_gauges([("metrics-dev-a", "modbus", True)])
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_device_connected",
            {"device_id": "metrics-dev-b", "protocol": "ads"},
        )
        is None
    )


def test_update_sink_queue_depths_sets_and_prunes_stale_series() -> None:
    metrics.update_sink_queue_depths({"metrics-sink-a": 3, "metrics-sink-b": 7})
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_sink_queue_depth", {"sink_name": "metrics-sink-a"}
        )
        == 3.0
    )
    metrics.update_sink_queue_depths({"metrics-sink-a": 1})
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_sink_queue_depth", {"sink_name": "metrics-sink-b"}
        )
        is None
    )


def test_render_returns_prometheus_text_format() -> None:
    body = metrics.render()
    assert isinstance(body, bytes)
    text = body.decode()
    assert "# HELP wind_hub_devices_total" in text
    assert "wind_hub_points_collected_total" in text
