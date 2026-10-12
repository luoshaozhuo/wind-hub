"""Collector 运行计数与近期事件状态。

该对象由 Collector Runtime/AcquisitionEngine 本地更新（实现
:class:`RuntimeMetricsPort`），是采集质量计数的唯一事实源；Server 只通过
低频 gRPC 读取快照，不再注入回调到 Collector Runtime。
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any


class CollectorMetricsState:
    """Collector 进程内运行指标与近期事件缓存。"""

    def __init__(self, event_capacity: int = 5000) -> None:
        self._lock = threading.RLock()
        self._points_total = 0
        self._points_bad = 0
        self._runs = 0
        self._failures = 0
        self._partial = 0
        self._missed = 0
        self._overruns = 0
        # 采集耗时与 poll 抖动的轻量统计（count/sum/max；无直方图，
        # 供快照推导均值与峰值）。
        self._duration_count = 0
        self._duration_sum = 0.0
        self._duration_max = 0.0
        self._jitter_count = 0
        self._jitter_sum = 0.0
        self._jitter_max = 0.0
        self._connect_failures = 0
        self._reconnects = 0
        self._device_connect_failures: dict[str, int] = defaultdict(int)
        self._device_reconnects: dict[str, int] = defaultdict(int)
        self._events: deque[dict[str, Any]] = deque(maxlen=event_capacity)

    def observe_collected(self, count: int) -> None:
        """记录一批采集点数量（引擎 on_points_collected 回调）。"""
        with self._lock:
            self._points_total += count

    def observe_bad(self, count: int) -> None:
        """记录一批 BAD 质量点数量（引擎 on_points_bad 回调）。"""
        with self._lock:
            self._points_bad += count

    def acquisition_run_finished(
        self,
        device_id: str,
        group: str,
        outcome: str,
        duration: float | None,
    ) -> None:
        """记录一次采集执行结果。"""
        with self._lock:
            self._runs += 1
            if duration is not None:
                self._duration_count += 1
                self._duration_sum += duration
                self._duration_max = max(self._duration_max, duration)
            if outcome == "failed":
                self._failures += 1
                self._event("acquisition_failed", device_id, f"{group}: collect failed")
            elif outcome == "partial":
                self._partial += 1
                self._event("acquisition_partial", device_id, f"{group}: partial batch")

    def acquisition_poll_stats(
        self,
        device_id: str,
        group: str,
        jitter: float,
        overrun: bool,
        missed: int,
    ) -> None:
        """记录 fixed-rate poll 时序统计。"""
        with self._lock:
            self._jitter_count += 1
            self._jitter_sum += jitter
            self._jitter_max = max(self._jitter_max, jitter)
            if overrun:
                self._overruns += 1
                self._event("poll_overrun", device_id, f"{group}: overrun")
            if missed:
                self._missed += missed
                self._event(
                    "missed_cycles",
                    device_id,
                    f"{group}: missed {missed} cycle(s)",
                )

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        """记录一次设备连接失败。"""
        with self._lock:
            self._connect_failures += 1
            self._device_connect_failures[device_id] += 1
            self._event("connect_failed", device_id, f"{protocol}: connection failed")

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        """记录一次设备重连成功。"""
        with self._lock:
            self._reconnects += 1
            self._device_reconnects[device_id] += 1
            self._event("reconnected", device_id, f"{protocol}: reconnected")

    def snapshot(self) -> dict[str, Any]:
        """返回 JSON 兼容指标快照。"""
        with self._lock:
            return {
                "counters": {
                    "points_total": self._points_total,
                    "points_bad": self._points_bad,
                    "acquisition_runs": self._runs,
                    "acquisition_failures": self._failures,
                    "acquisition_partial": self._partial,
                    "missed_cycles": self._missed,
                    "poll_overruns": self._overruns,
                    "connect_failures": self._connect_failures,
                    "reconnects": self._reconnects,
                },
                "acquisition_duration_seconds": {
                    "count": self._duration_count,
                    "avg": (
                        self._duration_sum / self._duration_count
                        if self._duration_count
                        else None
                    ),
                    "max": self._duration_max if self._duration_count else None,
                },
                "poll_jitter_seconds": {
                    "count": self._jitter_count,
                    "avg": (
                        self._jitter_sum / self._jitter_count if self._jitter_count else None
                    ),
                    "max": self._jitter_max if self._jitter_count else None,
                },
                "device_connect_failures": dict(self._device_connect_failures),
                "device_reconnects": dict(self._device_reconnects),
                "events": list(self._events),
            }

    def _event(self, kind: str, object_name: str, message: str) -> None:
        self._events.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "kind": kind,
                "object": object_name,
                "message": message,
            }
        )
