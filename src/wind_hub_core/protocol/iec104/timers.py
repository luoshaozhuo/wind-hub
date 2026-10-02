"""IEC104 t1/t2/t3 timer 管理器。

每个 timer 由独立 asyncio.Task 实现，到期后调用 session 注入的异步 callback。
重启同名 timer 会先取消旧 task，保证每类 timer 同时最多一个 runner。

t1：发送/确认超时；t2：延迟确认；t3：空闲保活。callback 异常记录后吞掉，
因为 timer task 不应因上层处理失败产生未检索异常。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# 保留的内部哨兵；不进入协议接口。
_STOP = object()


class IEC104Timers:
    """管理 IEC104 t1/t2/t3 的后台 task 生命周期。

    Args:
        t1: 发送确认超时秒数。
        t2: 延迟确认超时秒数。
        t3: 空闲保活超时秒数。
    """

    def __init__(
        self,
        t1: float = 15.0,
        t2: float = 10.0,
        t3: float = 20.0,
    ) -> None:
        self._t1 = t1
        self._t2 = t2
        self._t3 = t3

        # callback 由 session 构造后注入。
        self.on_t1_timeout: Callable[[], Awaitable[None]] | None = None
        self.on_t2_timeout: Callable[[], Awaitable[None]] | None = None
        self.on_t3_timeout: Callable[[], Awaitable[None]] | None = None

        # 每个 timer 对应一个后台 task。
        self._t1_task: asyncio.Task[object] | None = None
        self._t2_task: asyncio.Task[object] | None = None
        self._t3_task: asyncio.Task[object] | None = None

        # active 标志用于保证每类 timer 只有一个 runner。
        self._t1_active = False
        self._t2_active = False
        self._t3_active = False

    # ==================================================================
    # 公共接口
    # ==================================================================

    def start_t1(self) -> None:
        """重启 t1 发送确认 timer；已有 task 会先取消。"""
        self._start_timer(
            name="t1",
            delay=self._t1,
            task_attr="_t1_task",
            active_attr="_t1_active",
            callback_attr="on_t1_timeout",
        )

    def cancel_t1(self) -> None:
        """收到有效确认后取消 t1。"""
        self._cancel_timer("t1", "_t1_task")

    def start_t2(self) -> None:
        """重启 t2 延迟确认 timer。"""
        self._start_timer(
            name="t2",
            delay=self._t2,
            task_attr="_t2_task",
            active_attr="_t2_active",
            callback_attr="on_t2_timeout",
        )

    def cancel_t2(self) -> None:
        """发送确认后取消 t2。"""
        self._cancel_timer("t2", "_t2_task")

    def start_t3(self) -> None:
        """重启 t3 空闲保活 timer。"""
        self._start_timer(
            name="t3",
            delay=self._t3,
            task_attr="_t3_task",
            active_attr="_t3_active",
            callback_attr="on_t3_timeout",
        )

    def cancel_t3(self) -> None:
        """取消 t3。"""
        self._cancel_timer("t3", "_t3_task")

    async def stop_all(self) -> None:
        """取消并等待全部 timer task 退出；重复调用安全。"""
        for attr in ("_t1_task", "_t2_task", "_t3_task"):
            task: asyncio.Task[object] | None = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def reset_all(self) -> None:
        """同步发出全部 timer 的取消请求，不等待 task 退出。"""
        self._cancel_timer("t1", "_t1_task")
        self._cancel_timer("t2", "_t2_task")
        self._cancel_timer("t3", "_t3_task")

    # ==================================================================
    # 内部实现
    # ==================================================================

    def _start_timer(
        self,
        name: str,
        delay: float,
        task_attr: str,
        active_attr: str,
        callback_attr: str,
    ) -> None:
        # 同名 timer 重启前必须先取消旧 task。
        self._cancel_timer(name, task_attr)

        task = asyncio.ensure_future(self._timer_runner(name, delay, active_attr, callback_attr))
        setattr(self, task_attr, task)

    def _cancel_timer(self, name: str, task_attr: str) -> None:
        task: asyncio.Task[object] | None = getattr(self, task_attr, None)
        if task is not None and not task.done():
            task.cancel()

    async def _timer_runner(
        self,
        name: str,
        delay: float,
        active_attr: str,
        callback_attr: str,
    ) -> None:
        try:
            setattr(self, active_attr, True)
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        finally:
            setattr(self, active_attr, False)

        # timer 到期后调用注入 callback。
        cb: Callable[[], Awaitable[None]] | None = getattr(self, callback_attr, None)
        if cb is not None:
            try:
                await cb()
            except Exception:
                logger.exception("IEC104 timer %s callback raised", name)
