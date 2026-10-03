"""Server Quality/System Health 使用的监控查询端口与快照模型。"""

from __future__ import annotations

from dataclasses import dataclass
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
    points_total: int
    points_bad: int
    acquisition_runs: int
    acquisition_failures: int
    acquisition_partial: int
    missed_cycles: int
    poll_overruns: int
    connect_failures: int
    reconnects: int


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


class MonitoringHistoryPort(Protocol):
    """System Health / Quality 共用的采样历史视图。"""

    @property
    def started_at(self) -> datetime: ...

    def capture_now(self) -> HostSnapshot: ...

    def history(self, since: datetime) -> list[HostSnapshot]: ...


class MonitoringSnapshotPort(MonitoringHistoryPort, Protocol):
    """Server 低频 Collector 运行态缓存与显式刷新端口。"""

    async def refresh_now(self) -> HostSnapshot: ...

    def runtime_status(self) -> dict[str, object]: ...

    def devices_snapshot(self) -> list[dict[str, object]]: ...

    def sinks_snapshot(self) -> list[dict[str, object]]: ...

    def tasks_snapshot(self) -> list[dict[str, object]]: ...

    def counters_snapshot(self) -> CounterSnapshot: ...

    def device_counts(self, device_id: str) -> tuple[int, int]: ...

    def events_since(self, since: datetime) -> list[MonitoringEvent]: ...
