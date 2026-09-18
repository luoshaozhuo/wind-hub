"""Prometheus metrics — process-level singletons and helpers.

Gauges reflect engine *state* and are overwritten on every ``/metrics``
scrape from a :class:`~wind_hub.domain.engine.scheduler.Scheduler` snapshot.
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
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, generate_latest

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


def update_gauges(
    devices_total_val: int,
    devices_connected_val: int,
    sinks_total_val: int,
    sinks_healthy_val: int,
) -> None:
    """Overwrite the four engine gauges from a scheduler snapshot."""
    devices_total.set(devices_total_val)
    devices_connected.set(devices_connected_val)
    sinks_total.set(sinks_total_val)
    sinks_healthy.set(sinks_healthy_val)


def render() -> bytes:
    """Render the current metrics in the Prometheus text exposition format."""
    return generate_latest()


__all__ = [
    "devices_total",
    "devices_connected",
    "sinks_total",
    "sinks_healthy",
    "points_collected_total",
    "commands_sent_total",
    "commands_failed_total",
    "sink_writes_total",
    "sink_write_failures_total",
    "sink_points_written_total",
    "update_gauges",
    "render",
]
