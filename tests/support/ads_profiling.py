"""D/E 组：锁竞争与线程池的**测试侧**测量原语。

不修改任何生产代码：

- ``ThreadLockProfiler`` / ``AsyncLockProfiler``：同接口替换实例上的锁对象，
  记录每次获取的**等待时长**与**持有时长**（秒）；
- ``ProfilingExecutor``：包裹 ``ThreadPoolExecutor``，记录每个任务的
  **排队延迟**（submit → 线程开始执行）、**服务时长**与在线程峰值，
  同时让 E 组可以精确指定 16/32/64/128 worker；
- ``summarize``：统一的 p50/p95/p99 统计，避免各处重复实现。

所有计时使用 ``time.monotonic`` / ``time.thread_time`` 不适用（跨线程），
故排队/服务均以 monotonic 墙钟差为准。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class LockSample:
    wait_s: float
    held_s: float
    acquired: bool  # acquire(timeout=) 超时放弃时为 False


@dataclass
class TaskSample:
    queue_s: float
    service_s: float
    label: str


class ThreadLockProfiler:
    """threading.Lock 的同接口替身，记录等待/持有时长。"""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self.samples: list[LockSample] = []
        self._held_start: float | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        t0 = time.monotonic()
        ok = self._inner.acquire(blocking, timeout)
        wait = time.monotonic() - t0
        if ok:
            self._held_start = time.monotonic()
        self.samples.append(LockSample(wait, 0.0, ok))
        return ok

    def release(self) -> None:
        held = time.monotonic() - (self._held_start or time.monotonic())
        for sample in reversed(self.samples):
            if sample.acquired and sample.held_s == 0.0:
                sample.held_s = held
                break
        self._held_start = None
        self._inner.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()

    def locked(self) -> bool:
        return self._inner.locked()


class AsyncLockProfiler:
    """asyncio.Lock 的同接口替身，记录等待/持有时长。"""

    def __init__(self) -> None:
        self._inner = asyncio.Lock()
        self.samples: list[LockSample] = []
        self._held_start: float | None = None

    async def acquire(self) -> bool:
        t0 = time.monotonic()
        await self._inner.acquire()
        self._held_start = time.monotonic()
        self.samples.append(LockSample(self._held_start - t0, 0.0, True))
        return True

    def release(self) -> None:
        held = time.monotonic() - (self._held_start or time.monotonic())
        if self.samples and self.samples[-1].held_s == 0.0:
            self.samples[-1].held_s = held
        self._held_start = None
        self._inner.release()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self.release()

    def locked(self) -> bool:
        return self._inner.locked()


class ProfilingExecutor(ThreadPoolExecutor):
    """测量 to_thread 任务排队与服务时长的 ThreadPoolExecutor 子类。

    必须是 ThreadPoolExecutor 真子类：Python 3.11 的
    ``loop.set_default_executor`` 做 isinstance 强制校验。安装后生产代码
    ``asyncio.to_thread`` 的调度无需任何改动即可被观测。
    """

    def __init__(self, max_workers: int, name_prefix: str = "ads-probe") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=name_prefix)
        self.max_workers = max_workers
        self.samples: list[TaskSample] = []
        self._active_count = 0
        self._counter_lock = threading.Lock()
        self.peak_active = 0
        self.submitted = 0

    def submit(  # type: ignore[override]
        self, fn: Callable[..., T], /, *args: Any, label: str = "cmd", **kwargs: Any
    ) -> Any:
        self.submitted += 1
        t_submit = time.monotonic()

        def measured() -> T:
            t_start = time.monotonic()
            with self._counter_lock:
                self._active_count += 1
                self.peak_active = max(self.peak_active, self._active_count)
            try:
                return fn(*args, **kwargs)
            finally:
                t_end = time.monotonic()
                with self._counter_lock:
                    self._active_count -= 1
                self.samples.append(TaskSample(t_start - t_submit, t_end - t_start, label))

        return super().submit(measured)

    @property
    def active(self) -> int:
        with self._counter_lock:
            return self._active_count


@dataclass
class LatencyRecorder:
    """按标签记录端到端延迟样本。"""

    samples: dict[str, list[float]] = field(default_factory=dict)

    def record(self, label: str, elapsed_s: float) -> None:
        self.samples.setdefault(label, []).append(elapsed_s)

    class Timer:
        def __init__(self, recorder: LatencyRecorder, label: str) -> None:
            self._recorder = recorder
            self._label = label
            self._t0 = 0.0

        def __enter__(self) -> LatencyRecorder.Timer:
            self._t0 = time.monotonic()
            return self

        def __exit__(self, *exc: object) -> None:
            self._recorder.record(self._label, time.monotonic() - self._t0)

    def time(self, label: str) -> LatencyRecorder.Timer:
        return LatencyRecorder.Timer(self, label)


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {
        "n": float(len(ordered)),
        "min": ordered[0],
        "mean": sum(ordered) / len(ordered),
        "p50": percentile(ordered, 0.50),
        "p95": percentile(ordered, 0.95),
        "p99": percentile(ordered, 0.99),
        "max": ordered[-1],
        "sum": sum(ordered),
    }


def lock_report(profiler: ThreadLockProfiler | AsyncLockProfiler) -> dict[str, Any]:
    """从锁探针汇总等待/持有统计（秒）。"""
    waits = [s.wait_s for s in profiler.samples if s.acquired]
    held = [s.held_s for s in profiler.samples if s.acquired]
    contended = [s.wait_s for s in profiler.samples if s.acquired and s.wait_s > 1e-4]
    return {
        "acquisitions": len(profiler.samples),
        "wait": summarize(waits),
        "held": summarize(held),
        "contended_count": len(contended),
        "contended_wait": summarize(contended),
        "max_wait": max((s.wait_s for s in profiler.samples), default=0.0),
    }


def executor_report(executor: ProfilingExecutor) -> dict[str, Any]:
    return {
        "max_workers": executor.max_workers,
        "submitted": executor.submitted,
        "peak_active_threads": executor.peak_active,
        "queue_delay": summarize([s.queue_s for s in executor.samples]),
        "service": summarize([s.service_s for s in executor.samples]),
    }
