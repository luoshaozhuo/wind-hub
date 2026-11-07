"""点表映射代次读写屏障：允许并行读，提交独占，写者优先。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

from core.application.errors import ProtocolError

_IN_CALLBACK: ContextVar[tuple[object, asyncio.Task[object] | None] | None] = ContextVar(
    "protocol_callback_barrier", default=None
)


class MappingBarrier:
    """读操作持有映射代次快照直到 I/O 结束；写操作排他地提交。

    只保护映射/IO 的交界，不替代协议 Session 的原生资源所有权。
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    @contextmanager
    def callback_scope(self) -> Iterator[None]:
        """标识回调上下文，用于阻止其与更新/关闭屏障互相等待。"""
        token = _IN_CALLBACK.set((self, asyncio.current_task()))
        try:
            yield
        finally:
            _IN_CALLBACK.reset(token)

    def _callback_in_current_task(self) -> bool:
        marker = _IN_CALLBACK.get()
        return marker is not None and marker[0] is self and marker[1] is asyncio.current_task()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
            if self._callback_in_current_task() and (self._writer_active or self._writers_waiting):
                raise ProtocolError(
                    "protocol callback cannot start I/O while a mapping writer is pending"
                )
            await self._condition.wait_for(
                lambda: not self._writer_active and self._writers_waiting == 0
            )
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        if self._callback_in_current_task():
            raise ProtocolError("mapping updates or close are forbidden inside protocol callbacks")
        async with self._condition:
            self._writers_waiting += 1
            try:
                await self._condition.wait_for(
                    lambda: not self._writer_active and self._readers == 0
                )
                self._writer_active = True
            finally:
                self._writers_waiting -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                self._writer_active = False
                self._condition.notify_all()
