"""点表映射代次读写屏障：允许并行读，提交独占，写者优先。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class MappingBarrier:
    """读操作持有映射代次快照直到 I/O 结束；写操作排他地提交。

    只保护映射/IO 的交界，不替代协议 Session 的原生资源所有权。
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._condition:
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
