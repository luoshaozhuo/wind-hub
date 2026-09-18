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


# ---------------------------------------------------------------------------
# 采集/连接可观测性（PrometheusRuntimeMetrics + 拉取侧 gauge）
#
# 指标是进程级单例：每个用例使用唯一标签值，断言增量而非绝对值，
# 不重置全局注册表。
# ---------------------------------------------------------------------------


def test_runtime_metrics_counts_success_partial_failed_exactly_once() -> None:
    """success/partial/failed 三种结局各自只累加对应计数器一次；
    runs 总数每次都加。"""
    hook = metrics.PrometheusRuntimeMetrics()
    labels = {"device_id": "metrics-dev-acq", "group": "g1"}
    keys = {
        "runs": "wind_hub_acquisition_runs_total",
        "failures": "wind_hub_acquisition_failures_total",
        "partial": "wind_hub_acquisition_partial_total",
    }
    before = {k: REGISTRY.get_sample_value(v, labels) or 0.0 for k, v in keys.items()}

    hook.acquisition_run_finished("metrics-dev-acq", "g1", "success", 0.01)
    hook.acquisition_run_finished("metrics-dev-acq", "g1", "partial", 0.02)
    hook.acquisition_run_finished("metrics-dev-acq", "g1", "failed", 0.03)

    after = {k: REGISTRY.get_sample_value(v, labels) or 0.0 for k, v in keys.items()}
    assert after["runs"] - before["runs"] == 3.0
    assert after["failures"] - before["failures"] == 1.0
    assert after["partial"] - before["partial"] == 1.0
    # 耗时直方图：3 次观测
    count = REGISTRY.get_sample_value("wind_hub_acquisition_duration_seconds_count", labels)
    assert count == 3.0


def test_runtime_metrics_duration_skipped_when_none() -> None:
    """duration 为 None 时不观测直方图（计数器仍累加）。"""
    hook = metrics.PrometheusRuntimeMetrics()
    labels = {"device_id": "metrics-dev-nodur", "group": "g1"}
    hook.acquisition_run_finished("metrics-dev-nodur", "g1", "success", None)
    assert REGISTRY.get_sample_value("wind_hub_acquisition_runs_total", labels) == 1.0
    assert (
        REGISTRY.get_sample_value("wind_hub_acquisition_duration_seconds_count", labels) is None
    )


def test_runtime_metrics_connect_failure_and_reconnect_counters() -> None:
    """connect 失败与 Runtime 驱动的重连成功按 device_id/protocol 标签计数。"""
    hook = metrics.PrometheusRuntimeMetrics()
    labels = {"device_id": "metrics-dev-conn", "protocol": "modbus"}
    before_f = (
        REGISTRY.get_sample_value("wind_hub_device_connect_failures_total", labels) or 0.0
    )
    before_r = REGISTRY.get_sample_value("wind_hub_device_reconnect_total", labels) or 0.0

    hook.device_connect_failed("metrics-dev-conn", "modbus")
    hook.device_connect_failed("metrics-dev-conn", "modbus")
    hook.device_reconnected("metrics-dev-conn", "modbus")

    after_f = REGISTRY.get_sample_value("wind_hub_device_connect_failures_total", labels)
    after_r = REGISTRY.get_sample_value("wind_hub_device_reconnect_total", labels)
    assert after_f - before_f == 2.0
    assert after_r - before_r == 1.0


def test_points_bad_counter_increments() -> None:
    """points_bad_total（协议采集 BAD 点）与 points_dropped（背压丢弃）分开计数。"""
    before = REGISTRY.get_sample_value("wind_hub_points_bad_total") or 0.0
    metrics.points_bad_total.inc(4)
    after = REGISTRY.get_sample_value("wind_hub_points_bad_total")
    assert after - before == 4.0


def test_update_device_gauges_sets_and_prunes_stale_series() -> None:
    """逐设备连通 gauge：按快照覆盖；热重载删除的设备序列被移除。"""
    metrics.update_device_gauges(
        [("metrics-dev-a", "modbus", True), ("metrics-dev-b", "ads", False)]
    )
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_device_connected", {"device_id": "metrics-dev-a", "protocol": "modbus"}
        )
        == 1.0
    )
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_device_connected", {"device_id": "metrics-dev-b", "protocol": "ads"}
        )
        == 0.0
    )

    # 设备 b 被热删除 → 下一次覆盖移除其序列
    metrics.update_device_gauges([("metrics-dev-a", "modbus", True)])
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_device_connected", {"device_id": "metrics-dev-b", "protocol": "ads"}
        )
        is None
    )


def test_update_sink_queue_depths_sets_and_prunes_stale_series() -> None:
    """sink 队列深度 gauge：按快照覆盖；删除的 sink 序列被移除。"""
    metrics.update_sink_queue_depths({"metrics-sink-a": 3, "metrics-sink-b": 7})
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_sink_queue_depth", {"sink_name": "metrics-sink-a"}
        )
        == 3.0
    )
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_sink_queue_depth", {"sink_name": "metrics-sink-b"}
        )
        == 7.0
    )

    metrics.update_sink_queue_depths({"metrics-sink-a": 1})
    assert (
        REGISTRY.get_sample_value(
            "wind_hub_sink_queue_depth", {"sink_name": "metrics-sink-b"}
        )
        is None
    )
