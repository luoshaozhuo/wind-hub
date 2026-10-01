"""MonitoringMetrics 计数测试。"""

from wind_hub.domain.model.point import PointValue, Quality
from wind_hub_server.infra.monitoring import MonitoringMetrics


def test_monitoring_metrics_counts_quality_events() -> None:
    metrics = MonitoringMetrics()
    metrics.observe_points(
        [
            PointValue(device_id="d", point_id="a", value=1, quality=Quality.GOOD),
            PointValue(device_id="d", point_id="b", value=None, quality=Quality.BAD),
        ]
    )
    metrics.acquisition_poll_stats("d", "fast", 0.1, True, 2)
    metrics.device_connect_failed("d", "ads")

    counters = metrics.counters()
    assert counters.points_total == 2
    assert counters.points_bad == 1
    assert counters.missed_cycles == 2
    assert counters.poll_overruns == 1
    assert counters.connect_failures == 1
