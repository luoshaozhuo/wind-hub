"""Bounded TCP reachability adapter for read-only Commander diagnostics."""

from __future__ import annotations

import asyncio


class TcpProbe:
    async def probe(self, host: str, port: int, timeout: float) -> None:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
