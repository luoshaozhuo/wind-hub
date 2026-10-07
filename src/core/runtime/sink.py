"""Sink Runtime。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType

from core.application import PointValue
from core.application.port import SinkPort
from core.config import SinkId

_SinkBatch = tuple[PointValue, ...]
_QueueItem = _SinkBatch | None


class BackpressurePolicy(StrEnum):
    """Sink 队列满时的处理策略。"""

    BLOCK = "block"
    DROP_NEW = "drop_new"
    DROP_OLDEST = "drop_oldest"


class SinkRuntime:
    """SinkPort 生命周期、队列与异步交付的唯一 owner。

    每个 Sink 拥有独立有界队列和消费者协程。正常情况下，慢 Sink 不直接阻塞
    其他 Sink 的实际写入；队列满后的行为由 BackpressurePolicy 显式决定。
    """

    def __init__(
        self,
        sinks: Mapping[SinkId, SinkPort],
        *,
        queue_capacity: int = 128,
        backpressure_policy: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST,
        on_write_error: Callable[[SinkId, BaseException], None] | None = None,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")

        self._sinks = dict(sinks)
        self._queue_capacity = queue_capacity
        self._backpressure_policy = backpressure_policy
        self._on_write_error = on_write_error
        self._queues: dict[SinkId, asyncio.Queue[_QueueItem]] = {
            sink_id: asyncio.Queue(maxsize=queue_capacity) for sink_id in self._sinks
        }
        self._consumers: dict[SinkId, asyncio.Task[None]] = {}
        self._running = False
        self._points_routed = 0
        self._points_dropped = 0

    @property
    def sinks(self) -> Mapping[SinkId, SinkPort]:
        """返回只读 Sink 实例索引。"""
        return MappingProxyType(self._sinks)

    @property
    def points_routed(self) -> int:
        """累计成功进入 Sink 队列的点数。"""
        return self._points_routed

    @property
    def points_dropped(self) -> int:
        """累计因背压策略丢弃的点数。"""
        return self._points_dropped

    def queue_depths(self) -> dict[SinkId, int]:
        """返回各 Sink 当前队列深度。"""
        return {sink_id: queue.qsize() for sink_id, queue in self._queues.items()}

    async def start(self) -> None:
        """打开全部 Sink，并在全部成功后启动独立消费者。"""
        if self._running:
            raise RuntimeError("sink runtime is already started")

        opened: list[SinkPort] = []
        try:
            for sink in self._sinks.values():
                await sink.open()
                opened.append(sink)
        except Exception:
            for sink in reversed(opened):
                try:
                    await sink.close()
                except Exception:
                    pass
            raise

        self._running = True
        for sink_id, sink in self._sinks.items():
            self._consumers[sink_id] = asyncio.create_task(
                self._consume(sink_id, sink)
            )

    async def dispatch(
        self,
        sink_ids: Sequence[SinkId],
        batch: Sequence[PointValue],
    ) -> None:
        """把同一批标准点值路由到各目标 Sink 的独立队列。"""
        if not batch:
            return
        if not self._running:
            raise RuntimeError("sink runtime is not started")

        immutable_batch = tuple(batch)
        for sink_id in sink_ids:
            queue = self._queues[sink_id]
            await self._enqueue(sink_id, queue, immutable_batch)

    async def stop(self) -> None:
        """排空已入队数据，停止消费者，再 flush/close 全部 Sink。"""
        if not self._running:
            return
        self._running = False

        first_error: Exception | None = None

        for queue in self._queues.values():
            await queue.join()

        for queue in self._queues.values():
            await queue.put(None)

        for task in self._consumers.values():
            try:
                await task
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        self._consumers.clear()

        for sink in reversed(tuple(self._sinks.values())):
            try:
                await sink.flush()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            try:
                await sink.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc

        if first_error is not None:
            raise first_error

    async def _enqueue(
        self,
        sink_id: SinkId,
        queue: asyncio.Queue[_QueueItem],
        batch: _SinkBatch,
    ) -> None:
        policy = self._backpressure_policy

        if policy is BackpressurePolicy.BLOCK:
            await queue.put(batch)
            self._points_routed += len(batch)
            return

        if policy is BackpressurePolicy.DROP_NEW:
            if queue.full():
                self._points_dropped += len(batch)
                return
            queue.put_nowait(batch)
            self._points_routed += len(batch)
            return

        if policy is BackpressurePolicy.DROP_OLDEST:
            while queue.full():
                try:
                    evicted = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if evicted is not None:
                    self._points_dropped += len(evicted)
                queue.task_done()
            queue.put_nowait(batch)
            self._points_routed += len(batch)
            return

        raise ValueError(
            f"unsupported backpressure policy '{policy}' for sink '{sink_id}'"
        )

    async def _consume(self, sink_id: SinkId, sink: SinkPort) -> None:
        queue = self._queues[sink_id]
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                try:
                    await sink.write(item)
                except Exception as exc:
                    if self._on_write_error is not None:
                        self._on_write_error(sink_id, exc)
            finally:
                queue.task_done()
