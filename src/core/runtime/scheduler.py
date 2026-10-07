"""Runtime 周期调度基础设施。"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable


class FixedRateHandle:
    """基于 monotonic clock 与绝对 deadline 的 fixed-rate 调度句柄。

    与 fixed-delay 不同，调度时刻始终对齐初始时刻加整数倍 interval；单实例
    串行执行，不重入。若一次执行跨越多个周期，只保留至多一次立即 catch-up，
    其余完整错过周期直接跳过，避免过载时形成补采风暴。
    """

    def __init__(
        self,
        interval: float,
        callback: Callable[[], Awaitable[None]],
        *,
        on_error: Callable[[BaseException], None] | None = None,
        on_stats: Callable[[float, bool, int], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be greater than zero")
        self._interval = interval
        self._callback = callback
        self._on_error = on_error
        self._on_stats = on_stats
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """启动调度；重复调用保持幂等。"""
        if self._task is not None:
            return
        self._closed = False
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        """停止调度并等待后台任务退出；重复调用保持幂等。"""
        task, self._task = self._task, None
        if task is None:
            return
        self._closed = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_deadline = loop.time()

        while not self._closed:
            now = loop.time()
            if now < next_deadline:
                await asyncio.sleep(next_deadline - now)

            scheduled = next_deadline
            actual = loop.time()
            try:
                await self._callback()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._on_error is not None:
                    self._on_error(exc)

            end = loop.time()
            next_deadline = scheduled + self._interval
            overrun = end > next_deadline
            missed = 0
            if overrun:
                lag = end - next_deadline
                missed = int(lag // self._interval)
                next_deadline += missed * self._interval

            if self._on_stats is not None:
                self._on_stats(actual - scheduled, overrun, missed)
