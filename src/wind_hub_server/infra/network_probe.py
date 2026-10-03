"""Web/应用诊断使用的 Linux 网络探测基础设施。"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import time

from wind_hub_core.validation.network import ping_host as ping_reachable
from wind_hub_server.application.port.network_probe import (
    PingProbeResult,
    PortProbeResult,
)


class NetworkProbe:
    """基于宿主网络栈的 Diagnostics 探测适配器。"""

    def expand_network(self, network: str, max_ips: int = 4096) -> list[str]:
        """展开 IPv4 CIDR/单 IP，并限制最多 4096 个地址。"""
        try:
            net = ipaddress.ip_network(network.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid network '{network}': {exc}") from exc
        ips = [str(ip) for ip in net.hosts()]
        if len(ips) > max_ips:
            raise ValueError(f"network expands to {len(ips)} IPs; limit is {max_ips}")
        return ips

    async def ping(self, host: str, timeout: float = 1.0) -> PingProbeResult:
        """执行共享 ICMP 探测，并在 Server 层补充耗时元数据。"""
        started = time.monotonic()
        reachable = await ping_reachable(host, timeout=timeout)
        return PingProbeResult(
            host=host,
            reachable=reachable,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    async def probe_port(
        self,
        host: str,
        port: int,
        timeout: float = 1.0,
    ) -> PortProbeResult:
        """TCP connect 探测：open/closed/timeout/unreachable。"""
        started = time.monotonic()
        state = "open"
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except ConnectionRefusedError:
            state = "closed"
        except TimeoutError:
            state = "timeout"
        except OSError:
            state = "unreachable"
        else:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        return PortProbeResult(port, state, (time.monotonic() - started) * 1000)
