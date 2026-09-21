"""Sink dispatch and backpressure logic for Runtime."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from wind_hub.domain.model.point import PointValue

if TYPE_CHECKING:
    from wind_hub.application.runtime.runtime import Runtime

logger = logging.getLogger(__name__)


class RuntimeSinkDispatcher:
    """Own sink queues, backpressure behavior, and sink consumer loop."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def dispatch(self, routed: dict[str, list[PointValue]]) -> None:
        """Route data batches into sink queues while enforcing backpressure."""
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
