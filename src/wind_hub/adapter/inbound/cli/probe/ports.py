"""probe ports 的 TCP connect 端口扫描（决策 4/6/7/9/10，仅 Linux）。

只做 TCP connect（决策 9：UDP 扫描不可靠且易被误判为攻击，不做）；
TCP connect 走正常协议栈，无需 root、无需 raw socket（决策 10）。
四态区分见 :class:`~wind_hub.adapter.inbound.cli.probe.ports_models.PortState`。
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging

from wind_hub.adapter.inbound.cli.probe.ports_models import (
    PortResult,
    PortScanResult,
    PortState,
    guess_service,
)
from wind_hub.adapter.inbound.cli.probe.scan_methods import check_linux
from wind_hub.config.ports_config import PortsConfig

logger = logging.getLogger(__name__)


async def scan_port(
    ip: str,
    port: int,
    timeout: float,
    config: PortsConfig | None = None,
) -> PortResult:
    """扫描单个端口，返回四态结果（决策 4 + step20 任务 0.1）。

    - connect 成功 → ``OPEN``；
    - ``ConnectionRefusedError``（收到 RST，主机能达但端口未监听）
      → ``CLOSED``；
    - ``TimeoutError``（无响应，可能被防火墙 DROP）→ ``TIMEOUT``；
    - ``OSError`` 且 errno 为 ``EHOSTUNREACH`` / ``ENETUNREACH``（本机
      路由表里没有到目标的路径）→ ``UNREACHABLE``——语义是「路由不
      存在」，与对端无响应完全不同，对现场诊断价值高；
    - 其他 ``OSError``（连接重置等）→ 保守归 ``TIMEOUT`` 并记 debug
      日志——这类异常同样意味着「无法确认端口在监听」，但语义上区别
      于明确的 RST。
    """
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
    except ConnectionRefusedError:
        return PortResult(
            port=port, state=PortState.CLOSED, service_guess=guess_service(port, config)
        )
    except TimeoutError:
        return PortResult(
            port=port, state=PortState.TIMEOUT, service_guess=guess_service(port, config)
        )
    except OSError as exc:
        if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            return PortResult(
                port=port, state=PortState.UNREACHABLE, service_guess=guess_service(port, config)
            )
        logger.debug("port scan %s:%d failed with %s — counted as timeout", ip, port, exc)
        return PortResult(
            port=port, state=PortState.TIMEOUT, service_guess=guess_service(port, config)
        )
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return PortResult(port=port, state=PortState.OPEN, service_guess=guess_service(port, config))


async def scan_ports(
    ip: str,
    ports: list[int],
    timeout: float = 1.0,
    concurrency: int = 128,
    config: PortsConfig | None = None,
) -> PortScanResult:
    """扫描一个 IP 的多个端口（Semaphore 限流，决策 6/7）。

    结果按端口号升序排列。``config`` 提供服务名映射（step24 配置化）；
    缺省用内置 :data:`~...ports_models.SERVICE_MAP`。

    Raises:
        ConfigError: 非 Linux 平台（决策 1）。
    """
    check_linux()
    semaphore = asyncio.Semaphore(concurrency)

    async def probe(port: int) -> PortResult:
        async with semaphore:
            return await scan_port(ip, port, timeout, config)

    results = await asyncio.gather(*(probe(p) for p in ports))
    ordered = sorted(results, key=lambda r: r.port)
    open_count = sum(1 for r in ordered if r.state is PortState.OPEN)
    return PortScanResult(ip=ip, ports=ordered, total=len(ordered), open_count=open_count)
