"""Unit tests for ``infra/metrics.py``.

验证对象：Prometheus 指标单例的 gauge 覆盖（``update_gauges``）、计数器累加与
``render`` 的文本输出。指标是进程级单例，因此这里通过 ``REGISTRY`` 读取当前值，
不重置注册表。
"""

from __future__ import annotations

from prometheus_client import REGISTRY

from wind_hub.infra import metrics


def test_update_gauges_sets_device_and_sink_counts() -> None:
    metrics.update_gauges(
        devices_total_val=3,
        devices_connected_val=2,
        sinks_total_val=4,
        sinks_healthy_val=1,
    )
    assert REGISTRY.get_sample_value("wind_hub_devices_total") == 3.0
    assert REGISTRY.get_sample_value("wind_hub_devices_connected") == 2.0
    assert REGISTRY.get_sample_value("wind_hub_sinks_total") == 4.0
    assert REGISTRY.get_sample_value("wind_hub_sinks_healthy") == 1.0


def test_render_returns_prometheus_text_format() -> None:
    metrics.update_gauges(1, 0, 1, 1)
    body = metrics.render()
    assert isinstance(body, bytes)
    text = body.decode()
    assert "# HELP wind_hub_devices_total" in text
    assert "wind_hub_devices_total 1.0" in text
    assert "wind_hub_points_collected_total" in text


def test_points_collected_counter_increments() -> None:
    before = REGISTRY.get_sample_value("wind_hub_points_collected_total")
    metrics.points_collected_total.inc(7)
    after = REGISTRY.get_sample_value("wind_hub_points_collected_total")
    assert after - before == 7.0


def test_command_counters_increment() -> None:
    before_s = REGISTRY.get_sample_value("wind_hub_commands_sent_total") or 0.0
    before_f = REGISTRY.get_sample_value("wind_hub_commands_failed_total") or 0.0
    metrics.commands_sent_total.inc()
    metrics.commands_failed_total.inc(2)
    after_s = REGISTRY.get_sample_value("wind_hub_commands_sent_total")
    after_f = REGISTRY.get_sample_value("wind_hub_commands_failed_total")
    assert after_s - before_s == 1.0
    assert after_f - before_f == 2.0


def test_sink_write_counters_increment_with_label() -> None:
    """决策 6：sink 写入计数带 sink_name 标签，按标签独立累计。"""
    labels = {"sink_name": "metrics-test-sink"}
    before_w = REGISTRY.get_sample_value("wind_hub_sink_writes_total", labels) or 0.0
    before_f = REGISTRY.get_sample_value("wind_hub_sink_write_failures_total", labels) or 0.0
    metrics.sink_writes_total.labels(sink_name="metrics-test-sink").inc(2)
    metrics.sink_write_failures_total.labels(sink_name="metrics-test-sink").inc()
    after_w = REGISTRY.get_sample_value("wind_hub_sink_writes_total", labels)
    after_f = REGISTRY.get_sample_value("wind_hub_sink_write_failures_total", labels)
    assert after_w - before_w == 2.0
    assert after_f - before_f == 1.0


def test_render_includes_sink_counters_after_label_use() -> None:
    metrics.sink_writes_total.labels(sink_name="metrics-render-sink").inc()
    text = metrics.render().decode()
    assert 'wind_hub_sink_writes_total{sink_name="metrics-render-sink"}' in text


def test_sink_points_written_counter_increment_with_label() -> None:
    """决策 0.2：吞吐计数按点数累加、带 sink_name 标签。"""
    labels = {"sink_name": "metrics-points-sink"}
    before = REGISTRY.get_sample_value("wind_hub_sink_points_written_total", labels) or 0.0
    metrics.sink_points_written_total.labels(sink_name="metrics-points-sink").inc(5)
    metrics.sink_points_written_total.labels(sink_name="metrics-points-sink").inc(3)
    after = REGISTRY.get_sample_value("wind_hub_sink_points_written_total", labels)
    assert after - before == 8.0
