"""IEC104 主动上送的本地监听注册与分发。

设计边界（与协议原生订阅严格区分）：

- 只做本地注册，不向从站注册任何通知，不轮询、不隐式总召；
- 所有上送样本进入**单一有界队列**，由**单一 worker** 顺序分发，
  避免「每监听者一个永久任务」；
- 队列满时丢最旧样本并计数告警，保证慢消费者不能阻塞底层协议接收；
- 业务回调异常隔离，只记录日志，不影响其他监听者与接收管线。

取消语义：``unlisten`` 返回后保证不再有该监听者的新回调；若取消时
恰好有一个回调正在执行，``unlisten`` 会等待它结束（有界超时）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from core.application.protocol_contract import ProtocolSample

logger = logging.getLogger(__name__)

ProtocolListenerCallback = Callable[[ProtocolSample], Awaitable[None]]


class _Listener:
    """单个监听注册：按 point_id 集合过滤（None = 全部已配置点）。"""

    __slots__ = ("id", "filter", "callback", "removed", "finished")

    def __init__(
        self,
        listener_id: int,
        point_filter: frozenset[str] | None,
        callback: ProtocolListenerCallback,
    ) -> None:
        self.id = listener_id
        self.filter = point_filter
        self.callback = callback
        self.removed = False
        # 每次回调开始 clear、结束 set；unlisten 等待在途回调收敛。
        self.finished = asyncio.Event()
        self.finished.set()


class ListenerRegistry:
    """单 worker、有界队列的监听分发表（仅在所属 asyncio loop 内访问）。"""

    QUEUE_CAPACITY = 1000

    def __init__(self, *, capacity: int | None = None) -> None:
        self._queue: asyncio.Queue[ProtocolSample] = asyncio.Queue(
            maxsize=capacity or self.QUEUE_CAPACITY
        )
        self._listeners: dict[int, _Listener] = {}
        self._next_id = 1
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._dropped = 0

    @property
    def dropped_count(self) -> int:
        """因队列满被丢弃的样本总数。"""
        return self._dropped

    @property
    def active(self) -> bool:
        """是否还有未取消的监听者。"""
        return bool(self._listeners)

    def add(
        self,
        point_ids: tuple[str, ...],
        callback: ProtocolListenerCallback,
    ) -> int:
        """登记一个监听者，返回监听 id；空选点表示监听全部已配置点。"""
        if self._closed:
            raise RuntimeError("listener registry is closed")
        listener_id = self._next_id
        self._next_id += 1
        point_filter = frozenset(dict.fromkeys(point_ids)) if point_ids else None
        self._listeners[listener_id] = _Listener(listener_id, point_filter, callback)
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._dispatch(), name="iec104-listen-worker"
            )
        return listener_id

    def offer(self, sample: ProtocolSample) -> None:
        """接收管线入口：入队一条样本；队列满时丢最旧并计数。

        永不阻塞调用方（协议接收线程/任务），也不在无监听者时排队。
        """
        if self._closed or not self._listeners:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.warning(
                    "IEC104 listener queue overflow — %d sample(s) dropped "
                    "(slow consumer)",
                    self._dropped,
                )
        self._queue.put_nowait(sample)

    async def remove(self, listener_id: int, *, timeout: float = 5.0) -> None:
        """取消监听（幂等），并等待可能正在执行的该监听者回调结束。"""
        listener = self._listeners.pop(listener_id, None)
        if listener is None:
            return
        listener.removed = True
        try:
            await asyncio.wait_for(listener.finished.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "IEC104 listener %s callback did not finish within %.1fs",
                listener_id,
                timeout,
            )

    async def close(self) -> None:
        """关闭分发表：摘光监听者、取消 worker、清空队列（幂等）。"""
        if self._closed:
            return
        self._closed = True
        self._listeners.clear()
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            if worker is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    async def _dispatch(self) -> None:
        while True:
            sample = await self._queue.get()
            try:
                await self._fan_out(sample)
            finally:
                self._queue.task_done()

    async def _fan_out(self, sample: ProtocolSample) -> None:
        # 快照监听者，分发期间被取消的监听者不再调用；回调顺序执行，
        # 单个慢回调会延迟其他监听者，但绝不阻塞协议接收（队列有界）。
        for listener in tuple(self._listeners.values()):
            if listener.removed:
                continue
            if listener.filter is not None and sample.point_id not in listener.filter:
                continue
            listener.finished.clear()
            try:
                await listener.callback(sample)
            except Exception:
                logger.exception(
                    "IEC104 listener callback failed for point '%s'", sample.point_id
                )
            finally:
                # 取消可能发生在回调期间：remove 正在等待同一个 event。
                listener.finished.set()
