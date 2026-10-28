"""IEC104 本地上送监听句柄。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from core.application.port.protocol import ListenHandle, ProtocolSampleCallback
from core.application.protocol_contract import ProtocolSample

if TYPE_CHECKING:
    from .driver import IEC104Driver

logger = logging.getLogger(__name__)


class _Listener(ListenHandle):
    def __init__(
        self,
        owner: IEC104Driver,
        point_ids: frozenset[str],
        callback: ProtocolSampleCallback,
    ):
        self.owner = owner
        self.point_ids = point_ids
        self.callback = callback
        self.queue: asyncio.Queue[tuple[ProtocolSample, ...]] = asyncio.Queue(maxsize=256)
        self.task = asyncio.create_task(self._run(), name="iec104-listener")
        self.failed = False

    def offer(self, samples: tuple[ProtocolSample, ...]) -> None:
        if self.failed or self.task.done():
            return
        filtered = tuple(x for x in samples if x.point_id in self.point_ids)
        if not filtered:
            return
        try:
            self.queue.put_nowait(filtered)
        except asyncio.QueueFull:
            self.failed = True
            self.task.cancel()
            logger.error("IEC104 listener overflow: invalidated to avoid silent data loss")

    async def _run(self) -> None:
        while True:
            try:
                await self.callback(await self.queue.get())
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failed = True
                logger.exception("IEC104 listener callback failed; listener invalidated")
                return

    async def close(self) -> None:
        self.failed = True
        if not self.task.done():
            self.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=5.0)
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
            except TimeoutError:
                raise TimeoutError("IEC104 listener callback failed to stop") from None
        self.owner._listeners.discard(self)
