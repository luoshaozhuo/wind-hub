"""Server Prometheus exposition backed by cached Collector snapshots.

Server 不运行采集 Runtime，不在本地累加采集事件。所有 Collector 运行计数由
MonitoringService 的最近一次远端快照覆盖；Server 只维护可安全覆盖的 Gauge。
"""

from __future__ import annotations

from prometheus_client import Gauge, generate_latest

devices_total = Gauge("wind_hub_devices_total", "Number of configured devices.")
devices_connected = Gauge(
    "wind_hub_devices_connected",
    "Number of devices currently reporting a healthy connection.",
)
sinks_total = Gauge("wind_hub_sinks_total", "Number of configured sinks.")
sinks_healthy = Gauge(
    "wind_hub_sinks_healthy",
    "Number of sinks currently reporting healthy.",
)
device_connected = Gauge(
    "wind_hub_device_connected",
    "Per-device connection state (1 = connected, 0 = disconnected).",
    labelnames=["device_id", "protocol"],
)
sink_queue_depth = Gauge(
    "wind_hub_sink_queue_depth",
    "Current depth of each sink dispatch queue.",
    labelnames=["sink_name"],
)

points_collected_total = Gauge(
    "wind_hub_points_collected_total",
    "Collector cumulative point values collected.",
)
points_routed_total = Gauge(
    "wind_hub_points_routed_total",
    "Collector cumulative point values routed to sinks.",
)
points_dropped_total = Gauge(
    "wind_hub_points_dropped_total",
    "Collector cumulative point values dropped before delivery.",
)
points_bad_total = Gauge(
    "wind_hub_points_bad_total",
    "Collector cumulative BAD-quality point values.",
)
acquisition_runs_total = Gauge(
    "wind_hub_acquisition_runs_total",
    "Collector cumulative acquisition runs.",
)
acquisition_failures_total = Gauge(
    "wind_hub_acquisition_failures_total",
    "Collector cumulative failed acquisition runs.",
)
acquisition_partial_total = Gauge(
    "wind_hub_acquisition_partial_total",
    "Collector cumulative partial acquisition runs.",
)
poll_overrun_total = Gauge(
    "wind_hub_poll_overrun_total",
    "Collector cumulative poll overruns.",
)
poll_missed_cycles_total = Gauge(
    "wind_hub_poll_missed_cycles_total",
    "Collector cumulative missed poll cycles.",
)
device_connect_failures_total = Gauge(
    "wind_hub_device_connect_failures_total",
    "Collector cumulative device connect failures.",
)
device_reconnect_total = Gauge(
    "wind_hub_device_reconnect_total",
    "Collector cumulative successful reconnects.",
)

_device_gauge_labels: set[tuple[str, str]] = set()
_sink_depth_labels: set[str] = set()


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def update_runtime_gauges(
    status: dict[str, object],
    counters: dict[str, int],
) -> None:
    """用 Monitoring 缓存覆盖 Server 暴露的 Collector 聚合指标。"""
    devices_total.set(_as_int(status.get("device_count")))
    devices_connected.set(_as_int(status.get("devices_connected")))
    sinks_total.set(_as_int(status.get("sink_count")))
    sinks_healthy.set(_as_int(status.get("sinks_healthy")))
    points_collected_total.set(_as_int(status.get("points_collected")))
    points_routed_total.set(_as_int(status.get("points_routed")))
    points_dropped_total.set(_as_int(status.get("points_dropped")))
    points_bad_total.set(counters.get("points_bad", 0))
    acquisition_runs_total.set(counters.get("acquisition_runs", 0))
    acquisition_failures_total.set(counters.get("acquisition_failures", 0))
    acquisition_partial_total.set(counters.get("acquisition_partial", 0))
    poll_overrun_total.set(counters.get("poll_overruns", 0))
    poll_missed_cycles_total.set(counters.get("missed_cycles", 0))
    device_connect_failures_total.set(counters.get("connect_failures", 0))
    device_reconnect_total.set(counters.get("reconnects", 0))


def update_device_gauges(devices: list[tuple[str, str, bool]]) -> None:
    """按当前快照覆盖逐设备连通 Gauge，并移除陈旧标签。"""
    current = {(device_id, protocol) for device_id, protocol, _ in devices}
    for device_id, protocol in _device_gauge_labels - current:
        device_connected.remove(device_id, protocol)
    _device_gauge_labels.clear()
    _device_gauge_labels.update(current)
    for device_id, protocol, connected in devices:
        device_connected.labels(device_id=device_id, protocol=protocol).set(
            1 if connected else 0
        )


def update_sink_queue_depths(depths: dict[str, int]) -> None:
    """按当前快照覆盖 Sink 队列深度 Gauge，并移除陈旧标签。"""
    current = set(depths)
    for sink_name in _sink_depth_labels - current:
        sink_queue_depth.remove(sink_name)
    _sink_depth_labels.clear()
    _sink_depth_labels.update(current)
    for sink_name, depth in depths.items():
        sink_queue_depth.labels(sink_name=sink_name).set(depth)


def render() -> bytes:
    """返回 Prometheus text exposition。"""
    return generate_latest()


__all__ = [
    "update_runtime_gauges",
    "update_device_gauges",
    "update_sink_queue_depths",
    "render",
]
