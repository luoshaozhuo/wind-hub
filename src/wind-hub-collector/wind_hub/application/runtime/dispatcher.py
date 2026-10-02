"""Runtime 的 Sink 派发与背压策略。

本对象拥有各 Sink queue 的入队策略，但不负责 Sink open/close。drop_new/drop_old
会显式累计丢弃点数；block 通过 await queue.put() 向采集任务施加背压。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from wind_hub.domain.model.point import PointValue

if TYPE_CHECKING:
    from wind_hub.application.runtime.runtime import Runtime

logger = logging.getLogger(__name__)


class RuntimeSinkDispatcher:
    """封装 Runtime 的 Sink queue 派发与背压决策。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """按 Sink 名称路由批次并执行背压策略。

        Args:
            routed: sink_name 到 PointValue 批次的映射。
        """
        for sink_name, batch in routed.items():
            if not batch:
                continue
            queue = self._runtime._queues.get(sink_name)
            if queue is None:
                continue
            await self._handle_backpressure(queue, batch, sink_name)

    async def _handle_backpressure(
        self,
        queue: asyncio.Queue[list[PointValue]],
        batch: list[PointValue],
        sink_name: str,
    ) -> None:
        policy = self._runtime._config.backpressure_policy

        if policy == "drop_new":
            if queue.full():
                self._runtime._points_dropped += len(batch)
                logger.warning(
                    "Sink '%s' queue full (%d) — dropping new batch (%d points)",
                    sink_name,
                    queue.maxsize,
                    len(batch),
                )
                return
            await queue.put(batch)
            self._runtime._points_routed += len(batch)

        elif policy == "drop_old":
            while queue.full():
                try:
                    evicted = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._runtime._points_dropped += len(evicted)
            await queue.put(batch)
            self._runtime._points_routed += len(batch)

        elif policy == "block":
            await queue.put(batch)
            self._runtime._points_routed += len(batch)
