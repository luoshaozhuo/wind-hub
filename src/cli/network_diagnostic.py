"""独立、显式参数的基础网络诊断工具。"""

from __future__ import annotations

import asyncio
import socket
from time import perf_counter
from typing import Any


async def tcp(host: str, port: int, *, timeout: float = 3.0) -> dict[str, Any]:
    if not 1 <= port <= 65535 or timeout <= 0:
        raise ValueError("port must be 1..65535 and timeout must be positive")
    start = perf_counter()
    async with asyncio.timeout(timeout):
        _, writer = await asyncio.open_connection(host, port)
        try:
            return {
                "status": "PASS",
                "host": host,
                "port": port,
                "elapsed_ms": round((perf_counter() - start) * 1000, 3),
            }
        finally:
            writer.close()
            await writer.wait_closed()


async def resolve(host: str, *, timeout: float = 3.0) -> dict[str, Any]:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(timeout):
        addresses = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return {
        "status": "PASS",
        "host": host,
        "addresses": sorted({item[4][0] for item in addresses}),
    }
