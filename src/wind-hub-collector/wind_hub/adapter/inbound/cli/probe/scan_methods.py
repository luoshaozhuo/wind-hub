"""probe scan 的三种扫描方法（决策 2-6，仅 Linux）。

- :func:`scan_arp` —— 读 ``/proc/net/arp``（决策 3，最快最准，同网段；
  读不到时回退 ``ip neigh show``）。ARP 表是内核已解析的邻居缓存，
  无需发包、无需 root。
- :func:`scan_icmp` —— 系统 ``ping -c 1 -W`` 并发探测（决策 4），可覆
  盖 ARP 缓存里没有的跨网段地址。ping 二进制不存在时由编排层降级
  （决策 6）。
- :func:`scan_tcp` —— 对常用工业协议端口做 TCP connect 兜底
  （决策 5），应对 ICMP 被防火墙禁用的网段。

平台约束（决策 1）：只实现 Linux 分支；:func:`check_linux` 在非 Linux
平台抛 :class:`ConfigError`，由编排层在扫描前调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import sys
from pathlib import Path

from wind_hub.adapter.inbound.cli.probe.scan_models import parse_network
from wind_hub.domain.model.errors import ConfigError

logger = logging.getLogger(__name__)

# ARP 表路径（模块级常量，测试可替换为临时文件）。
_PROC_NET_ARP = Path("/proc/net/arp")

# 内核版本信息路径（用于 WSL 检测，测试可替换）。
_PROC_VERSION = Path("/proc/version")

# TCP connect 兜底探测的常用工业协议端口（决策 5）：Modbus / IEC104 /
# ADS / OPC UA。
DEFAULT_TCP_PORTS: tuple[int, ...] = (502, 2404, 48898, 4840)


def check_linux() -> None:
    """平台约束（决策 1）：非 Linux 平台抛 ConfigError（不写 Windows 分支）。

    Raises:
        ConfigError: ``sys.platform`` 不是 ``"linux"``。
    """
    if sys.platform != "linux":
        raise ConfigError(f"probe scan only supports Linux, got platform '{sys.platform}'")


def is_wsl() -> bool:
    """检测是否运行在 WSL 环境（读 ``/proc/version`` 是否含 "microsoft"）。

    用于决策 0.2 的提示：WSL2 的 ``/proc/net/arp`` 只看到 WSL 虚拟交换
    机的邻居，扫宿主机所在物理网段时 ARP 层基本无贡献（ICMP/TCP 不受
    影响）。mirrored 网络模式从虚拟机内部无法可靠检测，因此本判定只要
    是 WSL 就返回 True，warning 文案里注明缓解方式。读不到文件时按
    非 WSL 处理。
    """
    try:
        return "microsoft" in _PROC_VERSION.read_text(encoding="ascii").lower()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# ARP（决策 3）
# ---------------------------------------------------------------------------


def _parse_arp_table(text: str) -> dict[str, str]:
    """解析 ``/proc/net/arp`` 文本为 ``{ip: mac}``（跳过表头与不完整条目）。

    行格式：``IP address  HW type  Flags  HW address  Mask  Device``；
    ``Flags == 0x2`` 表示条目完整（已完成 ARP 解析）。
    """
    entries: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        ip, _hw_type, flags, mac = parts[0], parts[1], parts[2], parts[3]
        if flags != "0x2":
            continue
        entries[ip] = mac
    return entries


async def _read_ip_neigh() -> dict[str, str]:
    """``/proc/net/arp`` 不可用时的回退：解析 ``ip neigh show`` 输出。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip",
            "neigh",
            "show",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (OSError, TimeoutError):
        return {}
    entries: dict[str, str] = {}
    for line in out.decode(errors="replace").splitlines():
        # 形如 "10.0.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:01 REACHABLE"
        parts = line.split()
        if "lladdr" not in parts:
            continue  # FAILED / INCOMPLETE 等无 MAC 条目
        idx = parts.index("lladdr")
        entries[parts[0]] = parts[idx + 1]
    return entries


async def scan_arp(network: str) -> dict[str, str]:
    """ARP 表扫描：返回目标网段内 ``{ip: mac}``。

    只反映内核邻居缓存里已有的条目——不通的 IP 不会因此变活，所以
    结果需与 ICMP/TCP 合并（由编排层负责）。
    """
    targets = set(parse_network(network))
    try:
        entries = _parse_arp_table(_PROC_NET_ARP.read_text(encoding="ascii"))
    except OSError:
        logger.info("cannot read %s — falling back to 'ip neigh show'", _PROC_NET_ARP)
        entries = await _read_ip_neigh()
    return {ip: mac for ip, mac in entries.items() if ip in targets}


# ---------------------------------------------------------------------------
# ICMP（决策 4）
# ---------------------------------------------------------------------------


async def scan_icmp(
    ips: list[str],
    timeout: float,
    concurrency: int,
) -> set[str]:
    """ICMP ping 扫描：对 ``ips`` 并发执行 ``ping -c 1 -W``，返回可达集合。

    单个 ping 的 ``-W`` 参数是整数秒（向上取整），进程本身再加
    ``timeout + 1s`` 的硬超时兜底。ping 二进制不存在（极简容器）时整
    批返回空集合——编排层负责提前检测并降级（决策 6）。
    """
    semaphore = asyncio.Semaphore(concurrency)
    wait_seconds = max(1, math.ceil(timeout))

    async def ping_one(ip: str) -> str | None:
        async with semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ping",
                    "-c",
                    "1",
                    "-W",
                    str(wait_seconds),
                    ip,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                returncode = await asyncio.wait_for(proc.wait(), timeout=timeout + 1.0)
            except (OSError, TimeoutError):
                return None
            return ip if returncode == 0 else None

    results = await asyncio.gather(*(ping_one(ip) for ip in ips))
    return {ip for ip in results if ip is not None}


# ---------------------------------------------------------------------------
# TCP connect 兜底（决策 5）
# ---------------------------------------------------------------------------


async def scan_tcp(
    ips: list[str],
    ports: list[int] | tuple[int, ...],
    timeout: float,
    concurrency: int,
) -> set[str]:
    """TCP connect 扫描：任一端口连通即认为 IP 占用，返回存活集合。"""
    semaphore = asyncio.Semaphore(concurrency)

    async def probe_one(ip: str) -> str | None:
        async with semaphore:
            for port in ports:
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port), timeout=timeout
                    )
                except (OSError, TimeoutError):
                    continue
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return ip
            return None

    results = await asyncio.gather(*(probe_one(ip) for ip in ips))
    return {ip for ip in results if ip is not None}
