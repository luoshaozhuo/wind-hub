"""Transparent TCP proxy that delays server-to-client responses.

Used to make a real Modbus slave genuinely slow on the wire: every response
chunk is held for ``response_delay`` seconds, so the Collector experiences
real poll overruns instead of a simulated clock.
"""

from __future__ import annotations

import asyncio
import contextlib


class DelayedTcpProxy:
    def __init__(
        self,
        target_host: str,
        target_port: int,
        response_delay: float,
    ) -> None:
        self._target = (target_host, target_port)
        self._delay = response_delay
        self._server: asyncio.AbstractServer | None = None
        self._channels: set[asyncio.Task[None]] = set()
        self._closing = False
        self.port: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        assert self._server.sockets
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
        # Server.close() stops accepting but leaves both live sockets and
        # handlers mid-accept open; cancel pump tasks explicitly and drain so
        # a peer reconnecting during shutdown cannot leave an untracked pump.
        for task in tuple(self._channels):
            task.cancel()
        while self._channels:
            await asyncio.gather(*tuple(self._channels), return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.close()
            return
        upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        if self._closing:
            writer.close()
            upstream_writer.close()
            return
        downstream = asyncio.create_task(self._pump(reader, upstream_writer, 0.0))
        upstream = asyncio.create_task(self._pump(upstream_reader, writer, self._delay))
        self._channels.update((downstream, upstream))
        for task in (downstream, upstream):
            task.add_done_callback(self._channels.discard)

    @staticmethod
    async def _pump(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        delay: float,
    ) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                if delay:
                    await asyncio.sleep(delay)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(ConnectionError):
                writer.close()
                await writer.wait_closed()
