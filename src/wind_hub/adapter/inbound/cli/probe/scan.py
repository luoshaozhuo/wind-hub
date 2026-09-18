"""probe scan 的组合编排（决策 2/6/7/10）。

:func:`scan_network` 把三种方法按「ARP → ICMP → TCP」顺序叠加，结果
合并去重（一个 IP 只出一条 :class:`ScanResult`，``detected_by`` 记录
所有命中它的方法）；主机名反查默认关闭（决策 10：DNS 反查慢且不可靠）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import shutil
import socket

from wind_hub.adapter.inbound.cli.probe.scan_methods import (
    DEFAULT_TCP_PORTS,
    check_linux,
    scan_arp,
    scan_icmp,
    scan_tcp,
)
from wind_hub.adapter.inbound.cli.probe.scan_models import ScanResult, parse_network

logger = logging.getLogger(__name__)

# 主机名反查超时（决策 10）。
_HOSTNAME_TIMEOUT = 0.5


async def _resolve_hostname(ip: str, timeout: float = _HOSTNAME_TIMEOUT) -> str | None:
    """DNS 反查单个 IP；超时或无 PTR 记录返回 ``None``。

    ``NI_NAMEREQD`` 要求必须解析出名字（否则 getnameinfo 会退回数字
    形式，对「是否有主机名」没有信息量）。
    """
    loop = asyncio.get_running_loop()
    try:
        # 事件循环版 getnameinfo 返回 (hostname, port) 二元组。
        resolved: tuple[str, str] = await asyncio.wait_for(
            loop.getnameinfo((ip, 0), socket.NI_NAMEREQD), timeout=timeout
        )
    except (OSError, TimeoutError):
        return None
    return resolved[0]


async def scan_network(
    network: str,
    timeout: float = 1.0,
    concurrency: int = 128,
    resolve_hostname: bool = False,
    use_icmp: bool = True,
    use_tcp: bool = True,
) -> list[ScanResult]:
    """组合扫描一个网段，返回占用 IP 的结果列表（按 IP 数值排序）。

    流程（决策 2）：平台检查 → ARP 表 → ICMP（ping 可用时，否则降级
    并记录日志，决策 6）→ TCP connect 兜底 → 合并去重 → 可选主机名
    反查。

    Raises:
        ConfigError: 非 Linux 平台，或网段描述非法/超限。
    """
    check_linux()
    ips = parse_network(network)

    # 1. ARP 表（同网段；唯一能提供 MAC 的方法）
    arp_map = await scan_arp(network)
    detected: dict[str, list[str]] = {ip: ["arp"] for ip in arp_map}

    # 2. ICMP（决策 6：ping 二进制缺失时降级为纯 TCP，记录日志）
    if use_icmp:
        if shutil.which("ping") is not None:
            for ip in await scan_icmp(ips, timeout, concurrency):
                detected.setdefault(ip, []).append("icmp")
            # 决策 0.1：ping 触发了目标网段的 ARP 解析，重读邻居缓存补齐
            # MAC（成本极低——只是再读一次 /proc/net/arp）；新解析出的地址
            # 也并入结果。
            refreshed = await scan_arp(network)
            for ip, mac in refreshed.items():
                if ip not in arp_map:
                    arp_map[ip] = mac
                    # 只补 MAC：已被 ICMP/TCP 发现的地址，detected_by 保持
                    # 不变（ARP 是 ping 顺带触发的解析，不算独立发现）；
                    # 完全未见过的邻居才以 "arp" 并入结果。
                    if ip not in detected:
                        detected[ip] = ["arp"]
        else:
            logger.warning("'ping' not found — ICMP scan skipped, falling back to TCP-only")

    # 3. TCP connect 兜底（ICMP 被禁的网段）
    if use_tcp:
        for ip in await scan_tcp(ips, DEFAULT_TCP_PORTS, timeout, concurrency):
            detected.setdefault(ip, []).append("tcp")

    # 4. 合并：按 IP 数值排序，ARP 提供 MAC
    by_ip_value = sorted(detected.items(), key=lambda kv: int(ipaddress.IPv4Address(kv[0])))
    results = [
        ScanResult(
            ip=ip,
            alive=True,
            mac=arp_map.get(ip),
            detected_by=methods,
        )
        for ip, methods in by_ip_value
    ]

    # 5. 可选主机名反查（决策 10）
    if resolve_hostname:
        names = await asyncio.gather(*(_resolve_hostname(r.ip) for r in results))
        results = [
            ScanResult(
                ip=r.ip,
                alive=r.alive,
                mac=r.mac,
                hostname=name,
                detected_by=r.detected_by,
            )
            for r, name in zip(results, names, strict=True)
        ]

    return results
