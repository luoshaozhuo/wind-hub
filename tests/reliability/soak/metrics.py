"""Soak / 负载验收的指标采集（复用 ``tests.collector.perf`` 的 /proc 资源采样）。

与 perf 框架的分工：perf 关注「协议 × 网络场景」的延迟/吞吐基准（NullSink、
单设备、tc netem）；soak 关注「目标负载形态」的长期正确性——实际采样节拍、
抖动、错过周期、队列积压、任务数泄漏、sink 侧丢失/重复。两者共享同一份
/proc 资源读取与百分位实现，本模块只做增量。

周期口径：engine observer 每收到一个批次记一次「该设备完成一轮采集」，
相邻批次时间差即实际采样间隔；间隔超过 ``missed_factor`` × 目标间隔记一次
错过周期。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from dataclasses import dataclass, field
from typing import Any

from tests.performance.collector import (
    _percentile,
    _read_cpu_seconds,
    _read_fd_count,
    _read_memory_mb,
)

#: 实际间隔超过目标间隔的该倍数即记为一次错过周期（容忍正常调度抖动）。
DEFAULT_MISSED_FACTOR = 1.5

#: 单任务保留的间隔样本上限。超出后隔点抽稀（时间均匀、无分布偏移），
#: 24h × 200 任务的 soak 也不会让样本列表吃掉内存；错过周期数仍按全量
#: 在线精确计数，不受抽稀影响。
INTERVAL_SAMPLE_CAP = 5_000


def append_bounded(samples: list[float], value: float, cap: int) -> None:
    """追加样本并保持列表有界：超上限时隔点抽稀（有效步长随运行时长自适应）。"""
    samples.append(value)
    if len(samples) > cap:
        del samples[::2]


@dataclass
class CycleStats:
    """单个采集任务在测量窗口内的节拍统计。"""

    task_id: str
    target_interval_s: float
    cycles: int
    interval_mean_s: float
    interval_p50_s: float
    interval_p99_s: float
    jitter_s: float
    """实际间隔的标准差——节拍稳定性的核心指标。"""
    missed_cycles: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "target_interval_s": self.target_interval_s,
            "cycles": self.cycles,
            "interval_mean_s": self.interval_mean_s,
            "interval_p50_s": self.interval_p50_s,
            "interval_p99_s": self.interval_p99_s,
            "jitter_s": self.jitter_s,
            "missed_cycles": self.missed_cycles,
        }


@dataclass
class SoakMetrics:
    """一次 soak 运行的完整验收指标。"""

    profile: str
    duration_s: float
    cycles: list[CycleStats]
    points_collected: int
    points_routed: int
    points_dropped: int
    throughput_pps: float
    sink_received: int
    """sink wrapper 实际收到（``write()`` 成功）的点数（仅测量窗口）。"""
    sink_duplicates: int
    """sink 收到的 (device_id, point_id, timestamp) 完全重复的点数（仅测量窗口）。"""
    sink_latency_p50_ms: float
    sink_latency_p99_ms: float
    """单点「采集时间戳 → sink write 返回」耗时的百分位。"""
    write_commands: int = 0
    write_failures: int = 0
    queue_depth_max: int = 0
    asyncio_tasks_max: int = 0
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    fd_count: int = 0
    reconnect_count: int = 0
    reconnect_time_ms: float = 0.0
    sink_received_total: int = 0
    """含预热的全程收到点数——外部介质（topic/表）核验必须用这个口径，
    因为 warmup 写入同样落在外部介质里。"""
    memory_start_mb: float = 0.0
    """测量窗口起点的 RSS——长 soak 的内存泄漏判定基线。"""
    memory_end_mb: float = 0.0
    asyncio_tasks_start: int = 0
    asyncio_tasks_end: int = 0
    """窗口起止的 asyncio 任务数——任务泄漏判定（起点在 runtime 启动后采样）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "duration_s": self.duration_s,
            "cycles": [c.to_dict() for c in self.cycles],
            "points_collected": self.points_collected,
            "points_routed": self.points_routed,
            "points_dropped": self.points_dropped,
            "throughput_pps": self.throughput_pps,
            "sink_received": self.sink_received,
            "sink_duplicates": self.sink_duplicates,
            "sink_received_total": self.sink_received_total,
            "sink_latency_p50_ms": self.sink_latency_p50_ms,
            "sink_latency_p99_ms": self.sink_latency_p99_ms,
            "write_commands": self.write_commands,
            "write_failures": self.write_failures,
            "queue_depth_max": self.queue_depth_max,
            "asyncio_tasks_max": self.asyncio_tasks_max,
            "cpu_percent": self.cpu_percent,
            "memory_mb": self.memory_mb,
            "fd_count": self.fd_count,
            "reconnect_count": self.reconnect_count,
            "reconnect_time_ms": self.reconnect_time_ms,
            "memory_start_mb": self.memory_start_mb,
            "memory_end_mb": self.memory_end_mb,
            "asyncio_tasks_start": self.asyncio_tasks_start,
            "asyncio_tasks_end": self.asyncio_tasks_end,
        }


@dataclass
class _CycleTracker:
    """单任务的相邻批次间隔记录。

    ``intervals`` 是有界样本（见 :func:`append_bounded`），供均值/抖动/
    百分位使用；``total`` / ``missed`` 是全量精确计数。
    """

    target_interval_s: float
    last_at: float | None = None
    intervals: list[float] = field(default_factory=list)
    total: int = 0
    missed: int = 0


class SoakMetricsCollector:
    """soak 指标采集器：节拍 + sink + 资源/任务数周期采样 + 重连。"""

    def __init__(
        self,
        task_intervals: dict[str, float],
        *,
        missed_factor: float = DEFAULT_MISSED_FACTOR,
        sample_interval_s: float = 1.0,
    ) -> None:
        self._trackers = {
            task_id: _CycleTracker(target) for task_id, target in task_intervals.items()
        }
        self._missed_factor = missed_factor
        self._sample_interval_s = sample_interval_s
        self._queue_depths: list[int] = []
        self._task_counts: list[int] = []
        self._cpu_samples: list[tuple[float, float]] = []  # (wall, cpu_s)
        self._memory_mb: list[float] = []
        self._fd_counts: list[int] = []
        self._reconnects_ms: list[float] = []
        self._sample_task: asyncio.Task[None] | None = None
        self._queue_depth_provider: Any = None

    def set_queue_depth_provider(self, provider: Any) -> None:
        """注入队列深度来源（``Callable[[], dict[str, int]]``，取各 sink 最大值）。"""
        self._queue_depth_provider = provider

    async def start(self) -> None:
        self._take_resource_sample()
        self._sample_task = asyncio.create_task(self._sample_loop())

    async def stop(self) -> None:
        if self._sample_task is not None:
            self._sample_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sample_task
            self._sample_task = None
        self._take_resource_sample()

    def reset_measurement(self) -> None:
        """预热结束后调用：清空全部样本，资源基线重置为当前值。"""
        for tracker in self._trackers.values():
            tracker.last_at = None
            tracker.intervals.clear()
            tracker.total = 0
            tracker.missed = 0
        self._queue_depths.clear()
        self._task_counts.clear()
        self._cpu_samples.clear()
        self._memory_mb.clear()
        self._fd_counts.clear()
        self._reconnects_ms.clear()
        self._take_resource_sample()

    def record_batch(self, task_id: str) -> None:
        """engine observer 回调：某任务完成一轮采集（批次到达派发前）。"""
        tracker = self._trackers.get(task_id)
        if tracker is None:
            return
        now = time.perf_counter()
        if tracker.last_at is not None:
            interval = now - tracker.last_at
            tracker.total += 1
            if interval > tracker.target_interval_s * self._missed_factor:
                tracker.missed += 1
            append_bounded(tracker.intervals, interval, INTERVAL_SAMPLE_CAP)
        tracker.last_at = now

    def record_reconnect(self, duration_ms: float) -> None:
        self._reconnects_ms.append(duration_ms)

    def build_cycles(self) -> list[CycleStats]:
        """把间隔样本聚合为各任务的节拍统计。"""
        stats: list[CycleStats] = []
        for task_id, tracker in self._trackers.items():
            intervals = sorted(tracker.intervals)
            target = tracker.target_interval_s
            mean = sum(intervals) / len(intervals) if intervals else 0.0
            variance = (
                sum((iv - mean) ** 2 for iv in intervals) / len(intervals) if intervals else 0.0
            )
            stats.append(
                CycleStats(
                    task_id=task_id,
                    target_interval_s=target,
                    cycles=tracker.total + (1 if tracker.last_at is not None else 0),
                    interval_mean_s=mean,
                    interval_p50_s=_percentile(intervals, 50),
                    interval_p99_s=_percentile(intervals, 99),
                    jitter_s=math.sqrt(variance),
                    missed_cycles=tracker.missed,
                )
            )
        return stats

    def resource_summary(self) -> dict[str, float]:
        """CPU 取窗口平均，内存/FD/队列/任务数取峰值。"""
        cpu_percent = 0.0
        if len(self._cpu_samples) >= 2:
            (w0, c0), (w1, c1) = self._cpu_samples[0], self._cpu_samples[-1]
            if w1 > w0:
                cpu_percent = (c1 - c0) / (w1 - w0) * 100.0
        return {
            "cpu_percent": cpu_percent,
            "memory_mb": max(self._memory_mb, default=0.0),
            "fd_count": max(self._fd_counts, default=0),
            "queue_depth_max": max(self._queue_depths, default=0),
            "asyncio_tasks_max": max(self._task_counts, default=0),
            "memory_start_mb": self._memory_mb[0] if self._memory_mb else 0.0,
            "memory_end_mb": self._memory_mb[-1] if self._memory_mb else 0.0,
            "asyncio_tasks_start": self._task_counts[0] if self._task_counts else 0,
            "asyncio_tasks_end": self._task_counts[-1] if self._task_counts else 0,
        }

    def reconnect_summary(self) -> tuple[int, float]:
        return len(self._reconnects_ms), max(self._reconnects_ms, default=0.0)

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sample_interval_s)
            self._take_resource_sample()

    def _take_resource_sample(self) -> None:
        self._cpu_samples.append((time.perf_counter(), _read_cpu_seconds()))
        self._memory_mb.append(_read_memory_mb())
        self._fd_counts.append(_read_fd_count())
        self._task_counts.append(len(asyncio.all_tasks()))
        if self._queue_depth_provider is not None:
            depths = self._queue_depth_provider()
            self._queue_depths.append(max(depths.values(), default=0))
