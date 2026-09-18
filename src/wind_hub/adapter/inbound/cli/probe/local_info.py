"""probe diagnose 的本机网络信息获取（决策 1，仅 Linux）。

用 iproute2 的 JSON 输出（``ip -j addr show`` / ``ip -j route show
default``）——格式稳定、带接口名与前缀长度，比 ``socket.getaddrinfo``
准确，且不引入 psutil 等新依赖（决策 1）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from typing import Any

from wind_hub.adapter.inbound.cli.probe.diagnose_models import LocalInfo
from wind_hub.adapter.inbound.cli.probe.scan_methods import check_linux
from wind_hub.domain.model.errors import ConfigError

# 单条 ip 命令的硬超时（秒）：本地命令，正常应在毫秒内返回。
_IP_CMD_TIMEOUT = 3.0


async def _run_ip_json(*args: str) -> list[dict[str, Any]]:
    """执行 ``ip -j <args...>`` 并把 stdout 解析为 JSON 数组。

    模块级函数，测试可直接 monkeypatch 替换，无需伪造 subprocess。

    Raises:
        ConfigError: ``ip`` 命令不存在、执行失败或输出不是合法 JSON。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip",
            "-j",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_IP_CMD_TIMEOUT)
    except FileNotFoundError as exc:
        raise ConfigError("需要 iproute2（'ip' 命令不可用，请安装 iproute2）") from exc
    except (OSError, TimeoutError) as exc:
        raise ConfigError(f"执行 'ip -j {' '.join(args)}' 失败: {exc}") from exc
    if proc.returncode != 0:
        raise ConfigError(f"'ip -j {' '.join(args)}' 退出码 {proc.returncode}")
    try:
        data = json.loads(out.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"'ip -j {' '.join(args)}' 输出不是合法 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError(f"'ip -j {' '.join(args)}' 输出格式异常（期望 JSON 数组）")
    return data


def _parse_addr_entries(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """从 ``ip -j addr show`` 的 JSON 提取 ``[(ip/prefix, interface), ...]``。

    只取 IPv4（``family == "inet"``），过滤回环接口（决策 1）。
    """
    ips: list[tuple[str, str]] = []
    for iface in entries:
        ifname = iface.get("ifname", "")
        if ifname == "lo":
            continue
        for addr in iface.get("addr_info", []):
            if addr.get("family") != "inet":
                continue
            ips.append((f"{addr['local']}/{addr['prefixlen']}", ifname))
    return ips


def _parse_default_gateway(entries: list[dict[str, Any]]) -> str | None:
    """从 ``ip -j route show default`` 的 JSON 提取第一个默认网关。"""
    for route in entries:
        gateway = route.get("gateway")
        if gateway:
            return str(gateway)
    return None


async def get_local_info() -> LocalInfo:
    """获取本机信息：hostname / 所有非回环接口的 IPv4（带前缀）/ 默认网关。

    Raises:
        ConfigError: 非 Linux 平台，或 ``ip`` 命令不可用/输出异常。
    """
    check_linux()
    addr_entries, route_entries = await asyncio.gather(
        _run_ip_json("addr", "show"),
        _run_ip_json("route", "show", "default"),
    )
    return LocalInfo(
        hostname=socket.gethostname(),
        ips=_parse_addr_entries(addr_entries),
        default_gateway=_parse_default_gateway(route_entries),
    )


def is_same_subnet(
    local_ips: list[tuple[str, str]],
    target_ip: str,
) -> tuple[bool, str | None]:
    """判断目标 IP 是否落在本机任一接口网段内（决策 4）。

    Args:
        local_ips: ``[(ip/prefix, interface), ...]``，见 :class:`LocalInfo`。
        target_ip: 目标设备 IP（必须是合法 IPv4）。

    Returns:
        ``(是否同网段, 匹配到的网段描述)``；不匹配时描述为 ``None``。
        无法解析的本机条目静默跳过（数据来自 ``ip`` 输出，正常不会发
        生；跳过比整批失败更符合诊断工具的容错取向）。

    Raises:
        ConfigError: ``target_ip`` 不是合法 IPv4 地址。
    """
    try:
        target = ipaddress.IPv4Address(target_ip)
    except ipaddress.AddressValueError as exc:
        raise ConfigError(f"目标地址 '{target_ip}' 不是合法 IPv4 地址") from exc
    for cidr, ifname in local_ips:
        try:
            network = ipaddress.ip_interface(cidr).network
        except ValueError:
            continue
        if target in network:
            return True, f"{network} ({ifname})"
    return False, None
