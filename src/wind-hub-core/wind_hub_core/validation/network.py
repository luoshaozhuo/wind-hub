"""主动验证使用的基础网络探测。

Ping 与 TCP 分开返回，便于现场区分三层网络不可达和协议端口不可达。
"""

from __future__ import annotations

import asyncio
import contextlib


async def ping_host(host: str, *, timeout: float = 1.0) -> bool:
    """使用系统 ping 工具检查主机可达性。

    目标部署环境为 Linux/openEuler。命令不存在、超时或返回非零均返回
    False，不把网络诊断失败伪装成异常中断。
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
    """检查 TCP 端口是否可建立连接。"""
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
