"""probe scan 的数据模型与网段解析——纯逻辑、无 I/O，可独立单测。

:class:`ScanResult` 是「一个占用 IP」的扫描结果；:func:`parse_network`
把用户输入的网段描述（CIDR 或起止范围）展开为具体 IP 列表，并做规模
上限保护（决策 11）。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from wind_hub.domain.model.errors import ConfigError

# 单次扫描的 IP 数上限（决策 11）：防止把 /8 之类的大网段展开成千万级
# 探测任务。
DEFAULT_MAX_IPS = 4096


@dataclass(frozen=True)
class ScanResult:
    """单个 IP 的扫描结果（只报告占用的 IP）。

    Attributes:
        ip: IPv4 地址。
        alive: 是否存活（输出列表中恒为 ``True``——未占用的 IP 不生成
            结果条目）。
        mac: MAC 地址（仅 ARP 路径能提供；ICMP/TCP 发现的为 ``None``）。
        hostname: 反查主机名（仅 ``--resolve-hostname`` 时填充）。
        detected_by: 检测到该 IP 的方法，按 ``arp → icmp → tcp`` 顺序
            累积（用于合并去重后仍保留来源信息，决策 2）。
    """

    ip: str
    alive: bool
    mac: str | None = None
    hostname: str | None = None
    detected_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON/YAML 输出用的字典形式。"""
        return {
            "ip": self.ip,
            "alive": self.alive,
            "mac": self.mac,
            "hostname": self.hostname,
            "detected_by": list(self.detected_by),
        }


def parse_network(network: str, max_ips: int = DEFAULT_MAX_IPS) -> list[str]:
    """解析网段描述为 IP 列表（决策 11）。

    支持两种写法：

    - CIDR：``10.0.1.0/24``（按惯例去掉网络地址与广播地址；``/32``
      与 ``/31`` 按 :mod:`ipaddress` 语义保留全部地址）；
    - 起止范围：``10.0.1.1-10.0.1.254``（闭区间）。

    单个裸 IP（``10.0.1.5``）等价于 ``/32``。

    Raises:
        ConfigError: 格式非法，或展开后的 IP 数超过 ``max_ips``。
    """
    network = network.strip()
    ips = _parse_range(network) if "-" in network else _parse_cidr(network)
    if len(ips) > max_ips:
        raise ConfigError(
            f"network '{network}' expands to {len(ips)} IPs, exceeding the "
            f"limit of {max_ips}; narrow the range or raise the limit"
        )
    return ips


def _parse_cidr(network: str) -> list[str]:
    """CIDR → IP 列表（不含网络/广播地址，/31、/32 除外）。"""
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError as exc:
        raise ConfigError(f"invalid network '{network}': {exc}") from exc
    return [str(ip) for ip in net.hosts()]


def _parse_range(network: str) -> list[str]:
    """``start-end`` 起止范围 → IP 列表（闭区间）。"""
    start_s, _, end_s = network.partition("-")
    try:
        start = ipaddress.IPv4Address(start_s.strip())
        end = ipaddress.IPv4Address(end_s.strip())
    except ValueError as exc:
        raise ConfigError(f"invalid IP range '{network}': {exc}") from exc
    if int(end) < int(start):
        raise ConfigError(f"invalid IP range '{network}': end must be >= start")
    return [str(ipaddress.IPv4Address(i)) for i in range(int(start), int(end) + 1)]
