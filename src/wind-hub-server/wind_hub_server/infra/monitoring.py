"""Quality 与 System Health 共用的进程内监控事实源。"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from wind_hub_server.application.port.monitoring import (
    CounterSnapshot,
    HostSnapshot,
    MonitoringEvent,
)
from wind_hub.application.runtime.runtime import Runtime
from wind_hub_core.model.point import PointValue, Quality


class MonitoringMetrics:
    """RuntimeMetricsPort 的内存统计实现，同时记录近期事件。"""

    def __init__(self, event_capacity: int = 5000) -> None:
        self._lock = threading.RLock()
        self._points_total = 0
        self._points_bad = 0
        self._runs = 0
        self._failures = 0
        self._partial = 0
        self._missed = 0
        self._overruns = 0
        self._connect_failures = 0
        self._reconnects = 0
        self._device_connect_failures: dict[str, int] = defaultdict(int)
        self._device_reconnects: dict[str, int] = defaultdict(int)
        self._instance_failures: dict[tuple[str, str], int] = defaultdict(int)
        self._instance_missed: dict[tuple[str, str], int] = defaultdict(int)
        self._events: deque[MonitoringEvent] = deque(maxlen=event_capacity)

    def observe_points(self, values: list[PointValue]) -> None:
        """记录真实采集批次的点数与 BAD 点。"""
        with self._lock:
            self._points_total += len(values)
            self._points_bad += sum(1 for value in values if value.quality is Quality.BAD)

    def acquisition_run_finished(
        self, device_id: str, group: str, outcome: str, duration: float | None
    ) -> None:
        with self._lock:
            self._runs += 1
            if outcome == "failed":
                self._failures += 1
                self._instance_failures[(device_id, group)] += 1
                self._event("acquisition_failed", device_id, f"{group}: collect failed")
            elif outcome == "partial":
                self._partial += 1
                self._event("acquisition_partial", device_id, f"{group}: partial batch")

    def acquisition_poll_stats(
        self, device_id: str, group: str, jitter: float, overrun: bool, missed: int
    ) -> None:
        with self._lock:
            if overrun:
                self._overruns += 1
                self._event("poll_overrun", device_id, f"{group}: overrun")
            if missed:
                self._missed += missed
                self._instance_missed[(device_id, group)] += missed
                self._event(
                    "missed_cycles", device_id, f"{group}: missed {missed} cycle(s)"
                )

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        with self._lock:
            self._connect_failures += 1
            self._device_connect_failures[device_id] += 1
            self._event(
                "connect_failed", device_id, f"{protocol}: connection failed"
            )

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        with self._lock:
            self._reconnects += 1
            self._device_reconnects[device_id] += 1
            self._event("reconnected", device_id, f"{protocol}: reconnected")

    def counters(self) -> CounterSnapshot:
        """返回进程累计计数快照。"""
        with self._lock:
            return CounterSnapshot(
                points_total=self._points_total,
                points_bad=self._points_bad,
                acquisition_runs=self._runs,
                acquisition_failures=self._failures,
                acquisition_partial=self._partial,
                missed_cycles=self._missed,
                poll_overruns=self._overruns,
                connect_failures=self._connect_failures,
                reconnects=self._reconnects,
            )

    def device_counts(self, device_id: str) -> tuple[int, int]:
        """返回设备 connect failure / reconnect 累计数。"""
        with self._lock:
            return (
                self._device_connect_failures[device_id],
                self._device_reconnects[device_id],
            )

    def events_since(self, since: datetime) -> list[MonitoringEvent]:
        """返回时间窗口内的事件，最新在前。"""
        with self._lock:
            return [event for event in reversed(self._events) if event.timestamp >= since]

    def _event(self, kind: str, object_name: str, message: str) -> None:
        self._events.append(
            MonitoringEvent(datetime.now(UTC), kind, object_name, message)
        )


class RuntimeMetricsTarget(Protocol):
    """Runtime metrics 事件接收端口的结构化本地协议。"""

    def acquisition_run_finished(
        self, device_id: str, group: str, outcome: str, duration: float | None
    ) -> None: ...

    def acquisition_poll_stats(
        self, device_id: str, group: str, jitter: float, overrun: bool, missed: int
    ) -> None: ...

    def device_connect_failed(self, device_id: str, protocol: str) -> None: ...

    def device_reconnected(self, device_id: str, protocol: str) -> None: ...


class CompositeRuntimeMetrics:
    """把同一 Runtime 事件扇出到多个 metrics sink。"""

    def __init__(self, *targets: RuntimeMetricsTarget) -> None:
        self._targets = targets

    def acquisition_run_finished(
        self, device_id: str, group: str, outcome: str, duration: float | None
    ) -> None:
        for target in self._targets:
            target.acquisition_run_finished(device_id, group, outcome, duration)

    def acquisition_poll_stats(
        self, device_id: str, group: str, jitter: float, overrun: bool, missed: int
    ) -> None:
        for target in self._targets:
            target.acquisition_poll_stats(device_id, group, jitter, overrun, missed)

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        for target in self._targets:
            target.device_connect_failed(device_id, protocol)

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        for target in self._targets:
            target.device_reconnected(device_id, protocol)


class MonitoringService:
    """每分钟采一份 Runtime + Host 快照，最多保留 30 天。"""

    def __init__(
        self,
        runtime: Runtime,
        metrics: MonitoringMetrics,
        *,
        interval: float = 60.0,
        retention_days: int = 30,
    ) -> None:
        self._runtime = runtime
        self._metrics = metrics
        self._interval = interval
        max_samples = max(2, int(retention_days * 86400 / interval) + 2)
        self._history: deque[HostSnapshot] = deque(maxlen=max_samples)
        self._task: asyncio.Task[None] | None = None
        self._started_at = datetime.now(UTC)
        self._last_wall = time.monotonic()
        self._last_proc_cpu = self._proc_cpu_seconds()
        self._last_host_cpu = self._host_cpu_ticks()

    @property
    def started_at(self) -> datetime:
        return self._started_at

    async def start(self) -> None:
        """启动后台采样；幂等。"""
        if self._task is not None and not self._task.done():
            return
        self.capture_now()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止后台采样；幂等。"""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def capture_now(self) -> HostSnapshot:
        """同步采集当前主机/Runtime 快照并追加历史。"""
        now = datetime.now(UTC)
        memory_used, memory_total = self._memory_gb()
        disk = shutil.disk_usage("/")
        snapshot = HostSnapshot(
            timestamp=now,
            cpu_host_pct=self._host_cpu_pct(),
            cpu_process_pct=self._process_cpu_pct(),
            cpu_temp_c=self._cpu_temp(),
            memory_used_gb=memory_used,
            memory_total_gb=memory_total,
            process_rss_gb=self._process_rss_gb(),
            disk_free_gb=disk.free / 1024**3,
            disk_total_gb=disk.total / 1024**3,
            points_collected=self._runtime.points_collected,
            points_routed=self._runtime.points_routed,
            points_dropped=self._runtime.points_dropped,
            counters=self._metrics.counters(),
        )
        self._history.append(snapshot)
        return snapshot

    def history(self, since: datetime) -> list[HostSnapshot]:
        """返回窗口内样本；若窗口内为空，至少返回一个当前样本。"""
        rows = [row for row in self._history if row.timestamp >= since]
        if not rows:
            rows = [self.capture_now()]
        return rows

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self.capture_now()

    @staticmethod
    def _memory_gb() -> tuple[float | None, float | None]:
        try:
            fields = MonitoringService._proc_fields(Path("/proc/meminfo"))
            total = float(fields["MemTotal"].split()[0]) * 1024
            available = float(fields["MemAvailable"].split()[0]) * 1024
            return (total - available) / 1024**3, total / 1024**3
        except (OSError, KeyError, ValueError):
            return None, None

    @staticmethod
    def _process_rss_gb() -> float | None:
        try:
            fields = MonitoringService._proc_fields(Path("/proc/self/status"))
            return float(fields["VmRSS"].split()[0]) * 1024 / 1024**3
        except (OSError, KeyError, ValueError):
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return float(rss) * 1024 / 1024**3 if rss else None

    @staticmethod
    def _proc_fields(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition(":")
            if sep:
                result[key] = value.strip()
        return result

    @staticmethod
    def _proc_cpu_seconds() -> float:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime

    def _process_cpu_pct(self) -> float | None:
        now = time.monotonic()
        cpu = self._proc_cpu_seconds()
        wall_delta = now - self._last_wall
        cpu_delta = cpu - self._last_proc_cpu
        self._last_wall = now
        self._last_proc_cpu = cpu
        if wall_delta <= 0:
            return None
        cores = max(1, os.cpu_count() or 1)
        return max(0.0, min(100.0, cpu_delta / wall_delta / cores * 100.0))

    @staticmethod
    def _host_cpu_ticks() -> tuple[int, int] | None:
        try:
            line = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0]
            values = [int(value) for value in line.split()[1:]]
        except (OSError, ValueError, IndexError):
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    def _host_cpu_pct(self) -> float | None:
        current = self._host_cpu_ticks()
        previous, self._last_host_cpu = self._last_host_cpu, current
        if current is None or previous is None:
            return None
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100.0))

    @staticmethod
    def _cpu_temp() -> float | None:
        for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
            try:
                raw = float(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                continue
            value = raw / 1000.0 if raw > 1000 else raw
            if 0 < value < 150:
                return value
        return None
