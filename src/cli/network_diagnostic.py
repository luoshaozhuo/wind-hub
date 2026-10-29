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


async def _command(argv: list[str], timeout: float) -> dict[str, Any]:
    """运行只读网络命令，超时或取消时回收子进程。"""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    output = stdout.decode("utf-8", errors="replace").strip()
    error = stderr.decode("utf-8", errors="replace").strip()
    return {
        "status": "PASS" if process.returncode == 0 else "FAIL",
        "returncode": process.returncode,
        "output": output[:4000],
        "error": error[:1000],
    }


async def ping(host: str, *, count: int = 4, timeout: float = 1.0) -> dict[str, Any]:
    """向明确主机执行 Linux ping，结果不伪装为协议健康。"""
    if not 1 <= count <= 20 or not 0 < timeout <= 30:
        raise ValueError("count must be 1..20 and timeout 0..30")
    result = await _command(
        ["ping", "-n", "-c", str(count), "-W", str(max(1, int(timeout))), host],
        count * (timeout + 1) + 2,
    )
    return {"host": host, "count": count, **result}


async def route(host: str, *, timeout: float = 3.0) -> dict[str, Any]:
    """检查 Linux 对目标主机的实际路由选择。"""
    result = await _command(["ip", "-j", "route", "get", host], timeout)
    return {"host": host, **result}
