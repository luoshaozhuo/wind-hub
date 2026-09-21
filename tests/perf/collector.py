"""性能指标采集（决策 6/7）。

**偏离说明（决策 7 的 psutil）**：spec 假设环境已有 psutil，实际未安装；
约束又禁止引入新依赖。资源采样改由 ``/proc`` 直读实现（Linux-only，
与本项目一致），指标口径不变：

- CPU：``/proc/self/stat`` 的 utime+stime 差值 ÷ 墙钟（psutil
  ``cpu_percent`` 同口径，单核 100% 上限、多核可超 100）；
- 内存：``/proc/self/status`` 的 VmRSS（取测量期峰值）；
- FD：``/proc/self/fd`` 目录项数（取峰值）。

三个读取函数是模块级 seam（单测可替换，等效 spec 的「mock psutil」）。

延迟口径：engine observer 在批次派发前收到通知，``utcnow -
PointValue.timestamp`` 即「采集 → 处理完成」的单点耗时。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _read_cpu_seconds() -> float:
    """本进程累计 CPU 时间（秒）：/proc/self/stat 的 utime + stime。"""
    with open("/proc/self/stat") as f:
        parts = f.read().split()
    # 字段 14/15（0-based 13/14）= utime / stime（单位 clock tick）。
    return (int(parts[13]) + int(parts[14])) / _CLK_TCK


def _read_memory_mb() -> float:
    """本进程 RSS（MiB）：/proc/self/status 的 VmRSS。"""
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    raise RuntimeError("/proc/self/status 中未找到 VmRSS")


def _read_fd_count() -> int:
    """本进程当前打开的 FD 数。"""
    return len(os.listdir("/proc/self/fd"))


class RuntimeStats(Protocol):
    """构建指标所需的运行时计数切片（与 ``Runtime`` 同名属性结构对齐）。"""

    @property
    def points_collected(self) -> int: ...

    @property
    def points_routed(self) -> int: ...

    @property
    def points_dropped(self) -> int: ...


@dataclass
class PerfMetrics:
    """一个场景 × 一个协议 的性能指标。"""

    protocol: str
    scenario: str
    duration_s: float
    points_collected: int
    points_routed: int
    points_dropped: int
    throughput_pps: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    cpu_percent: float
    memory_mb: float
    fd_count: int
    reconnect_count: int
    reconnect_time_ms: float
    """单次最长恢复时间（毫秒）——重连行为的最坏面。"""
    latency_samples_ms: list[float] = field(default_factory=list, repr=False)
    """升序延迟原始样本（step24：--runs N 聚合时合并样本重算百分位）。
    体积大，不进 ``to_dict`` / JSON 报告。"""

    def to_dict(self) -> dict[str, Any]:
        """JSON 报告用的字典形式。"""
        return {
            "protocol": self.protocol,
            "scenario": self.scenario,
            "duration_s": self.duration_s,
            "points_collected": self.points_collected,
            "points_routed": self.points_routed,
            "points_dropped": self.points_dropped,
            "throughput_pps": self.throughput_pps,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "latency_p99_ms": self.latency_p99_ms,
            "latency_max_ms": self.latency_max_ms,
            "cpu_percent": self.cpu_percent,
            "memory_mb": self.memory_mb,
            "fd_count": self.fd_count,
            "reconnect_count": self.reconnect_count,
            "reconnect_time_ms": self.reconnect_time_ms,
        }


@dataclass
class _ResourceSample:
    wall: float
    cpu_s: float
    memory_mb: float
    fd_count: int


@dataclass
class MetricsCollector:
    """性能指标采集器：延迟直方 + 资源周期采样 + 重连记录。"""

    sample_interval_s: float = 1.0
    _latencies_ms: list[float] = field(default_factory=list)
    _reconnects_ms: list[float] = field(default_factory=list)
    _samples: list[_ResourceSample] = field(default_factory=list)
    _sample_task: asyncio.Task[None] | None = field(default=None, init=False)

    async def start(self) -> None:
        """开始采集（打基线快照 + 启动周期采样任务）。"""
        self._samples = [self._take_sample()]
        self._sample_task = asyncio.create_task(self._sample_loop())

    async def stop(self) -> None:
        """停止采集（取消采样任务，补一个收尾快照）。"""
        if self._sample_task is not None:
            self._sample_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sample_task
            self._sample_task = None
        self._samples.append(self._take_sample())

    def reset_measurement(self) -> None:
        """预热结束后调用：清空延迟/重连样本，资源基线重置为当前值。"""
        self._latencies_ms.clear()
        self._reconnects_ms.clear()
        self._samples = [self._take_sample()]

    def record_latency(self, ms: float) -> None:
        """记录单点延迟（毫秒）。"""
        self._latencies_ms.append(ms)

    def record_reconnect(self, duration_ms: float) -> None:
        """记录一次重连恢复（毫秒）。"""
        self._reconnects_ms.append(duration_ms)

    def build_metrics(
        self,
        protocol: str,
        scenario: str,
        runtime_stats: RuntimeStats,
        duration_s: float,
    ) -> PerfMetrics:
        """把采样结果汇聚为最终指标。

        CPU 取整个测量窗口的平均利用率；内存/FD 取窗口内峰值；
        吞吐按 collected 计数 ÷ 测量时长。
        """
        lat = sorted(self._latencies_ms)
        cpu_percent = self._avg_cpu_percent()
        return PerfMetrics(
            protocol=protocol,
            scenario=scenario,
            duration_s=duration_s,
            points_collected=runtime_stats.points_collected,
            points_routed=runtime_stats.points_routed,
            points_dropped=runtime_stats.points_dropped,
            throughput_pps=runtime_stats.points_collected / duration_s if duration_s > 0 else 0.0,
            latency_p50_ms=_percentile(lat, 50),
            latency_p95_ms=_percentile(lat, 95),
            latency_p99_ms=_percentile(lat, 99),
            latency_max_ms=lat[-1] if lat else 0.0,
            cpu_percent=cpu_percent,
            memory_mb=max((s.memory_mb for s in self._samples), default=0.0),
            fd_count=max((s.fd_count for s in self._samples), default=0),
            reconnect_count=len(self._reconnects_ms),
            reconnect_time_ms=max(self._reconnects_ms, default=0.0),
            latency_samples_ms=lat,
        )

    async def _sample_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sample_interval_s)
            try:
                self._samples.append(self._take_sample())
            except Exception:
                logger.warning("perf 资源采样失败（跳过本次）", exc_info=True)

    def _take_sample(self) -> _ResourceSample:
        return _ResourceSample(
            wall=time.perf_counter(),
            cpu_s=_read_cpu_seconds(),
            memory_mb=_read_memory_mb(),
            fd_count=_read_fd_count(),
        )

    def _avg_cpu_percent(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        first, last = self._samples[0], self._samples[-1]
        wall_delta = last.wall - first.wall
        if wall_delta <= 0:
            return 0.0
        return (last.cpu_s - first.cpu_s) / wall_delta * 100.0


def _percentile(sorted_values: list[float], pct: float) -> float:
    """最近秩百分位；空列表返回 0.0。"""
    if not sorted_values:
        return 0.0
    rank = math.ceil(len(sorted_values) * pct / 100.0)
    return sorted_values[max(0, rank - 1)]
