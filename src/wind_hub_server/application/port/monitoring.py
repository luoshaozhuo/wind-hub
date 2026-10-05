"""Server Quality/System Health 使用的监控查询端口与快照模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class MonitoringEvent:
    timestamp: datetime
    kind: str
    object: str
    message: str


@dataclass(frozen=True)
class CounterSnapshot:
    points_total: int = 0
    points_bad: int = 0
    acquisition_runs: int = 0
    acquisition_failures: int = 0
    acquisition_partial: int = 0
    missed_cycles: int = 0
    poll_overruns: int = 0
    connect_failures: int = 0
    reconnects: int = 0


@dataclass(frozen=True)
class HostSnapshot:
    timestamp: datetime
    cpu_host_pct: float | None
    cpu_process_pct: float | None
    cpu_temp_c: float | None
    memory_used_gb: float | None
    memory_total_gb: float | None
    process_rss_gb: float | None
    disk_free_gb: float | None
    disk_total_gb: float | None
    points_collected: int
    points_routed: int
    points_dropped: int
    counters: CounterSnapshot


@dataclass(frozen=True)
class AcquisitionStatus:
    """单个采集实例的运行状态（Collector wire 与聚合快照共用）。"""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    running: bool
    consecutive_failures: int
    last_error: str | None
    last_duration: float | None


@dataclass(frozen=True)
class MetricsSnapshot:
    """Collector 累计指标快照（计数、逐设备计数与事件流）。"""

    counters: CounterSnapshot
    device_connect_failures: dict[str, int] = field(default_factory=dict)
    device_reconnects: dict[str, int] = field(default_factory=dict)
    events: list[MonitoringEvent] = field(default_factory=list)


@dataclass(frozen=True)
class RuntimeStatusSnapshot:
    """跨 Collector 聚合后的进程级 Runtime 状态。

    全字段默认值表示「尚无成功快照」的空态，与首次 refresh 前的语义一致。
    """

    running: bool = False
    device_count: int = 0
    sink_count: int = 0
    devices_connected: int = 0
    sinks_healthy: int = 0
    points_collected: int = 0
    points_routed: int = 0
    points_dropped: int = 0
    acquisitions: list[AcquisitionStatus] = field(default_factory=list)
    degraded: bool = False
    unavailable_workers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeviceRuntimeSnapshot:
    """跨 Collector 聚合后的单设备运行态。"""

    device_id: str
    protocol: str
    connected: bool
    consecutive_failures: int
    last_error: str | None


@dataclass(frozen=True)
class SinkRuntimeSnapshot:
    """跨 Collector 聚合后的单 Sink 运行态。"""

    name: str
    healthy: bool
    message: str | None
    queue_depth: int


@dataclass(frozen=True)
class TaskRuntimeSnapshot:
    """单个 Task 的 Collector 运行态与其 placement 归属。"""

    task_id: str
    device: str | None
    device_group: str | None
    point_group: str
    interval: float | None
    targets: list[str]
    enabled: bool
    runtime_state: str
    instance_count: int
    running_instances: int
    stopped_instances: int
    failed_instances: int
    assigned_worker_id: str


@dataclass(frozen=True)
class CollectorSnapshot:
    """一次跨 Collector 聚合的完整低频快照。"""

    runtime_status: RuntimeStatusSnapshot
    metrics: MetricsSnapshot
    devices: list[DeviceRuntimeSnapshot]
    sinks: list[SinkRuntimeSnapshot]
    tasks: list[TaskRuntimeSnapshot]


class MonitoringHistoryPort(Protocol):
    """System Health / Quality 共用的采样历史视图。"""

    @property
    def started_at(self) -> datetime: ...

    def capture_now(self) -> HostSnapshot: ...

    def history(self, since: datetime) -> list[HostSnapshot]: ...


class MonitoringSnapshotPort(MonitoringHistoryPort, Protocol):
    """Server 低频 Collector 运行态缓存与显式刷新端口。"""

    async def refresh_now(self) -> HostSnapshot: ...

    def runtime_status(self) -> RuntimeStatusSnapshot: ...

    def devices_snapshot(self) -> list[DeviceRuntimeSnapshot]: ...

    def sinks_snapshot(self) -> list[SinkRuntimeSnapshot]: ...

    def tasks_snapshot(self) -> list[TaskRuntimeSnapshot]: ...

    def counters_snapshot(self) -> CounterSnapshot: ...

    def device_counts(self, device_id: str) -> tuple[int, int]: ...

    def events_since(self, since: datetime) -> list[MonitoringEvent]: ...
