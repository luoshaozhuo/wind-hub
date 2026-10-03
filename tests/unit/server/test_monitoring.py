"""MonitoringMetrics 远端快照镜像测试。"""

from wind_hub_server.infra.monitoring import MonitoringMetrics


def test_monitoring_metrics_applies_remote_snapshot() -> None:
    metrics = MonitoringMetrics()
    metrics.apply_remote(
        {
            "counters": {
                "points_total": 10,
                "points_bad": 2,
                "acquisition_runs": 8,
                "acquisition_failures": 1,
                "acquisition_partial": 1,
                "missed_cycles": 3,
                "poll_overruns": 2,
                "connect_failures": 4,
                "reconnects": 5,
            },
            "device_connect_failures": {"d1": 4},
            "device_reconnects": {"d1": 5},
            "events": [
                {
                    "timestamp": "2026-10-03T10:00:00+00:00",
                    "kind": "connect_failed",
                    "object": "d1",
                    "message": "ads: connection failed",
                }
            ],
        }
    )

    counters = metrics.counters()
    assert counters.points_total == 10
    assert counters.points_bad == 2
    assert counters.acquisition_runs == 8
    assert counters.missed_cycles == 3
    assert counters.connect_failures == 4
    assert counters.reconnects == 5
    assert metrics.device_counts("d1") == (4, 5)
    events = metrics.events_since(
        __import__("datetime").datetime.fromisoformat("2026-10-03T09:00:00+00:00")
    )
    assert len(events) == 1
    assert events[0].kind == "connect_failed"
