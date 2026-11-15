"""Bounded TCP reachability probe for Collector Application diagnostics."""

from __future__ import annotations

import asyncio


class TcpNetworkProbe:
    async def probe(self, host: str, port: int, timeout: float) -> None:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
