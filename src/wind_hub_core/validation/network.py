"""诊断与协议验证共享的基础网络探测。

Ping 与 TCP 探测保持独立，便于现场区分 ICMP 不可达、TCP 端口不可达和更高层
协议失败。两个函数都返回布尔事实，不负责编排诊断流程，也不修改系统网络配置。

ping_host 会创建短生命周期子进程；tcp_port_open 会创建短生命周期 TCP
connection。所有正常失败路径都显式释放对应资源。
"""

from __future__ import annotations

import asyncio
import contextlib


async def ping_host(host: str, *, timeout: float = 1.0) -> bool:
    """调用系统 ping 检查主机可达性。

    Args:
        host: 目标主机名或 IP 地址。
        timeout: 单次探测超时，单位秒。

    Returns:
        ping 进程在超时内返回 0 时为 True；命令不存在、超时或非零退出均为 False。

    Notes:
        目标部署环境为 Linux/openEuler。超时时主动 kill 子进程并等待其回收，
        避免残留子进程。
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
        # 网络诊断工具不可用属于探测失败，不应中断整套诊断流程。
        return False

    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout + 0.5) == 0
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return False


async def tcp_port_open(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """检查目标 TCP 端口能否建立连接。

    Args:
        host: 目标主机名或 IP 地址。
        port: TCP 端口。
        timeout: 建连超时，单位秒。

    Returns:
        在超时内成功建立 TCP connection 时为 True，否则为 False。

    Notes:
        本函数只判断 TCP 建连，不代表 ADS/Modbus/IEC104 协议握手成功。
        成功建连后会在 finally 中关闭 writer 并等待 socket 关闭。
    """
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
            # 关闭阶段异常不能改变已经得到的端口探测事实。
            with contextlib.suppress(Exception):
                await writer.wait_closed()
