"""TCP black hole: accepts connections and never replies.

Used to deterministically exercise protocol connect/read deadlines (no data
ever arrives, so ``wait_for`` timeouts are not timing-dependent).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator


class TcpBlackhole:
    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._stop = asyncio.Event()
        self.port: int = 0

    async def start(self) -> None:
        async def hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await self._stop.wait()
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()

        def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.create_task(hold(reader, writer))
            self._handlers.add(task)
            task.add_done_callback(self._handlers.discard)

        self._server = await asyncio.start_server(on_connect, "127.0.0.1", 0)
        assert self._server.sockets
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._stop.set()
            self._server.close()
            await self._server.wait_closed()
            if self._handlers:
                await asyncio.gather(*self._handlers, return_exceptions=True)
            self._server = None


async def blackhole() -> AsyncIterator[TcpBlackhole]:
    hole = TcpBlackhole()
    await hole.start()
    try:
        yield hole
    finally:
        await hole.stop()
