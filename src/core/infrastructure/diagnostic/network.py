"""按需执行的基础网络诊断；不持有进程状态。"""

from __future__ import annotations

import asyncio
import contextlib


async def ping_host(host: str, *, timeout: float = 1.0) -> bool:
    """调用系统 ping 检查主机可达性；命令缺失/超时/非零退出均为 False。

    超时时主动 kill 子进程并等待回收，避免残留子进程。
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "ping",
            "-c",
            "1",
            "-W",
            str(max(1, int(timeout))),
            host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False

    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout + 0.5) == 0
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return False


async def tcp_port_open(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """检查目标 TCP 端口能否建立连接（不代表协议握手成功）。"""
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        return True
    except (OSError, TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


__all__ = ["ping_host", "tcp_port_open"]
