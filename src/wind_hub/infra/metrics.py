"""Prometheus metrics — process-level singletons and helpers.

Gauges reflect engine *state* and are overwritten on every ``/metrics``
scrape from a :class:`~wind_hub.application.runtime.Runtime` snapshot.
Counters accumulate *events* as they occur (a point value is collected, a
command is dispatched).

All metric objects are module globals so that every consumer — the
composition root wiring the collection callback and the ``/metrics`` route
rendering the response — shares the same registry.

反直觉的一点：计数器在 domain 采集循环里递增，但 domain 不得依赖 infra
（见 ``pyproject.toml`` 的 import-linter 契约）。因此这里的
``points_collected_total.inc`` / ``commands_sent_total.inc`` 通过组合根
注入为调度器/分发器的回调，而不是在 domain 层直接 ``import`` 本模块；
而 sink 实现位于 adapter 层（允许依赖 infra），故带标签的
``sink_writes_total`` / ``sink_write_failures_total`` 由各 sink 直接递增。
Runtime 侧事件（connect 失败、重连、collect 完成）经
:class:`PrometheusRuntimeMetrics`（结构化实现 application 层的
``RuntimeMetricsPort``）注入，application 层同样不 import 本模块。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Gauges — 每次 /metrics 拉取时用调度器快照覆盖
# ---------------------------------------------------------------------------

devices_total = Gauge(
    "wind_hub_devices_total",
    "Number of configured devices.",
)
devices_connected = Gauge(
    "wind_hub_devices_connected",
    "Number of devices currently reporting a healthy connection.",
)
sinks_total = Gauge(
    "wind_hub_sinks_total",
    "Number of configured sinks.",
)
sinks_healthy = Gauge(
    "wind_hub_sinks_healthy",
    "Number of sinks currently reporting healthy.",
)
# 逐设备连通状态（标签 device_id/protocol——均为配置值，基数受控）：
# 每次拉取时从 Runtime 快照覆盖；热重载删除的设备标签对会被移除。
device_connected = Gauge(
    "wind_hub_device_connected",
    "Per-device connection state (1 = connected, 0 = disconnected).",
    labelnames=["device_id", "protocol"],
)
# Sink 队列深度（队列归 Runtime 所有，SinkPort 不感知队列）：
# 每次拉取时从 Runtime 快照覆盖。
sink_queue_depth = Gauge(
    "wind_hub_sink_queue_depth",
    "Current depth of each sink dispatch queue.",
    labelnames=["sink_name"],
)

# ---------------------------------------------------------------------------
# Counters — 事件发生时累加
# ---------------------------------------------------------------------------

# NOTE: prometheus_client 会自动给 Counter 名称追加 ``_total`` 后缀，
# 因此这里的基础名不写 ``_total``，暴露出的指标名为 ``..._points_collected_total``。
points_collected_total = Counter(
    "wind_hub_points_collected",
    "Cumulative point values collected from devices.",
)
commands_sent_total = Counter(
    "wind_hub_commands_sent",
    "Cumulative write commands dispatched.",
)
commands_failed_total = Counter(
    "wind_hub_commands_failed",
    "Cumulative write commands that failed.",
)
# 带 sink_name 标签的 sink 写入计数（决策 6）：各 sink 实现（adapter 层，
# 允许依赖 infra）在 write() 成功/失败后直接递增；Kafka 的 broker-ack
# 失败（flush/reap 时发现）也计入 failures。
sink_writes_total = Counter(
    "wind_hub_sink_writes",
    "Cumulative successful sink write() calls.",
    labelnames=["sink_name"],
)
sink_write_failures_total = Counter(
    "wind_hub_sink_write_failures",
    "Cumulative failed sink write() calls and broker-ack rejections.",
    labelnames=["sink_name"],
)
# 吞吐视角（决策 0.2）：sink_writes_total 按 write() 调用计次，无法反映
# 点数吞吐（一批 1 点与 1000 点都计 1）；本计数器在 write() 成功后按
# len(batch) 累加实际写出的点数。
sink_points_written_total = Counter(
    "wind_hub_sink_points_written",
    "Cumulative point values successfully written by sinks.",
    labelnames=["sink_name"],
)
# 协议采集结果中的 BAD 质量点（数据质量问题）——与 points_dropped
# （Sink 派发/背压丢弃）语义不同，分开计数。
points_bad_total = Counter(
    "wind_hub_points_bad",
    "Cumulative point values collected with Quality.BAD.",
)
# 设备连接失败 / 重连成功计数（标签 device_id/protocol——配置值，基数受控；
# 不使用 error_message 等高基数字段作标签）。经 RuntimeMetricsPort 回调累加。
device_connect_failures_total = Counter(
    "wind_hub_device_connect_failures",
    "Cumulative failed device connect attempts.",
    labelnames=["device_id", "protocol"],
)
device_reconnect_total = Counter(
    "wind_hub_device_reconnect",
    "Cumulative successful reconnects driven by the Runtime ensure path.",
    labelnames=["device_id", "protocol"],
)
# 采集 Job 执行计数与耗时（标签 device_id/group——(设备, 分组) 即一个
# 调度 Job，基数受控）。duration 使用 prometheus-client 默认 bucket。
acquisition_runs_total = Counter(
    "wind_hub_acquisition_runs",
    "Cumulative acquisition collect runs.",
    labelnames=["device_id", "group"],
)
acquisition_failures_total = Counter(
    "wind_hub_acquisition_failures",
    "Cumulative failed acquisition collect runs.",
    labelnames=["device_id", "group"],
)
acquisition_partial_total = Counter(
    "wind_hub_acquisition_partial",
    "Cumulative acquisition runs with mixed GOOD/BAD results.",
    labelnames=["device_id", "group"],
)
acquisition_duration_seconds = Histogram(
    "wind_hub_acquisition_duration_seconds",
    "Acquisition collect run duration in seconds.",
    labelnames=["device_id", "group"],
)
# Fixed-rate poll 时序统计（标签 device_id/group——与采集 Job 同口径，
# 基数受控）。jitter = 实际启动时刻 − 计划时刻（monotonic）；overrun 为
# 本轮结束越过下一计划时刻的次数；missed 为跳过的完整周期数（catch-up
# 至多一次，不爆发补采）。
poll_jitter_seconds = Histogram(
    "wind_hub_poll_jitter_seconds",
    "Fixed-rate poll start jitter (actual - scheduled) in seconds.",
    labelnames=["device_id", "group"],
)
poll_overrun_total = Counter(
    "wind_hub_poll_overrun",
    "Cumulative poll cycles that finished past the next scheduled deadline.",
    labelnames=["device_id", "group"],
)
poll_missed_cycles_total = Counter(
    "wind_hub_poll_missed_cycles",
    "Cumulative scheduled poll cycles skipped after overrun (no catch-up burst).",
    labelnames=["device_id", "group"],
)


def update_gauges(
    devices_total_val: int,
    devices_connected_val: int,
    sinks_total_val: int,
    sinks_healthy_val: int,
) -> None:
    """Overwrite the four engine gauges from a runtime snapshot."""
    devices_total.set(devices_total_val)
    devices_connected.set(devices_connected_val)
    sinks_total.set(sinks_total_val)
    sinks_healthy.set(sinks_healthy_val)


# 带标签 gauge 的拉取侧覆盖：记录上次写入的标签组合，热重载删除的
# 设备/sink 在下一次拉取时移除其序列（Counter 无法删除，Gauge 可以）。
_device_gauge_labels: set[tuple[str, str]] = set()
_sink_depth_labels: set[str] = set()


def update_device_gauges(devices: list[tuple[str, str, bool]]) -> None:
    """按 ``[(device_id, protocol, connected)]`` 快照覆盖逐设备连通 gauge。

    热重载删除的设备，其标签序列在本次拉取时移除，避免残留陈旧序列。
    """
    current = {(device_id, protocol) for device_id, protocol, _ in devices}
    for device_id, protocol in _device_gauge_labels - current:
        device_connected.remove(device_id, protocol)
    _device_gauge_labels.clear()
    _device_gauge_labels.update(current)
    for device_id, protocol, connected in devices:
        device_connected.labels(device_id=device_id, protocol=protocol).set(1 if connected else 0)


def update_sink_queue_depths(depths: dict[str, int]) -> None:
    """按 ``{sink_name: depth}`` 快照覆盖 sink 队列深度 gauge。"""
    current = set(depths)
    for sink_name in _sink_depth_labels - current:
        sink_queue_depth.remove(sink_name)
    _sink_depth_labels.clear()
    _sink_depth_labels.update(current)
    for sink_name, depth in depths.items():
        sink_queue_depth.labels(sink_name=sink_name).set(depth)


class PrometheusRuntimeMetrics:
    """``RuntimeMetricsPort`` 的 Prometheus 实现——组合根注入 Runtime。

    结构化实现（不显式继承 application 层的 Protocol，避免 infra →
    application 依赖）：方法签名与端口契约一致，由装配处做类型契合。
    """

    def acquisition_run_finished(
        self, device_id: str, group: str, outcome: str, duration: float | None
    ) -> None:
        """累加一次 collect 的运行/失败/partial 计数并观测耗时。"""
        labels = {"device_id": device_id, "group": group}
        acquisition_runs_total.labels(**labels).inc()
        if outcome == "failed":
            acquisition_failures_total.labels(**labels).inc()
        elif outcome == "partial":
            acquisition_partial_total.labels(**labels).inc()
        if duration is not None:
            acquisition_duration_seconds.labels(**labels).observe(duration)

    def acquisition_poll_stats(
        self, device_id: str, group: str, jitter: float, overrun: bool, missed: int
    ) -> None:
        """观测一次 fixed-rate poll 的 jitter，并累加 overrun / missed 计数。"""
        labels = {"device_id": device_id, "group": group}
        poll_jitter_seconds.labels(**labels).observe(jitter)
        if overrun:
            poll_overrun_total.labels(**labels).inc()
        if missed:
            poll_missed_cycles_total.labels(**labels).inc(missed)

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        """累加一次 connect 失败。"""
        device_connect_failures_total.labels(device_id=device_id, protocol=protocol).inc()

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        """累加一次 Runtime 驱动的重连成功。"""
        device_reconnect_total.labels(device_id=device_id, protocol=protocol).inc()


def render() -> bytes:
    """Render the current metrics in the Prometheus text exposition format."""
    return generate_latest()


__all__ = [
    "devices_total",
    "devices_connected",
    "device_connected",
    "sinks_total",
    "sinks_healthy",
    "sink_queue_depth",
    "points_collected_total",
    "points_bad_total",
    "commands_sent_total",
    "commands_failed_total",
    "sink_writes_total",
    "sink_write_failures_total",
    "sink_points_written_total",
    "device_connect_failures_total",
    "device_reconnect_total",
    "acquisition_runs_total",
    "acquisition_failures_total",
    "acquisition_partial_total",
    "acquisition_duration_seconds",
    "poll_jitter_seconds",
    "poll_overrun_total",
    "poll_missed_cycles_total",
    "PrometheusRuntimeMetrics",
    "update_gauges",
    "update_device_gauges",
    "update_sink_queue_depths",
    "render",
]
