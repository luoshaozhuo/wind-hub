"""Server 网络诊断端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PingProbeResult:
    """ICMP 探测结果。"""

    host: str
    reachable: bool
    latency_ms: float


@dataclass(frozen=True)
class PortProbeResult:
    """TCP 端口探测结果。"""

    port: int
    state: str
    latency_ms: float


class NetworkProbePort(Protocol):
    """Diagnostics 所需的宿主网络探测能力。"""

    def expand_network(self, network: str, max_ips: int = 4096) -> list[str]:
        """展开 IPv4 CIDR/单 IP。"""
        ...

    async def ping(self, host: str, timeout: float = 1.0) -> PingProbeResult:
        """执行 ICMP 探测。"""
        ...

    async def probe_port(
        self,
        host: str,
        port: int,
        timeout: float = 1.0,
    ) -> PortProbeResult:
        """执行 TCP connect 探测。"""
        ...
