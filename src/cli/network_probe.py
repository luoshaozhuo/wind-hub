"""Bounded CLI network probes using Core's shared diagnostic primitives."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from typing import Any

from core.infrastructure.diagnostic.network import (
    ping_host,
    ping_probe as core_ping_probe,
    tcp_probe as core_tcp_probe,
)

MAX_HOSTS = 4096
MAX_CONCURRENCY = 256
INDUSTRIAL_PORTS = {
    102: "Siemens S7",
    502: "Modbus TCP",
    2404: "IEC 104",
    4840: "OPC UA",
    44818: "EtherNet/IP",
    48898: "ADS Router",
}


async def tcp_probe(host: str, port: int, timeout: float = 1.0) -> dict[str, Any]:
    ipaddress.ip_address(host)
    result = await core_tcp_probe(host, port, timeout=timeout)
    code = result.findings[0].code if result.findings else ""
    state = (
        "OPEN"
        if result.status.value == "PASS"
        else "TIMEOUT"
        if code == "TCP_CONNECT_TIMEOUT"
        else "CLOSED"
    )
    return {
        "host": host,
        "port": port,
        "tcp_state": state,
        "latency_ms": round(float(result.measurements.get("connect_ms", 0)), 2),
    }


async def industrial_probe(
    host: str, timeout: float = 1.0, ports: tuple[int, ...] | None = None
) -> list[dict[str, Any]]:
    selected = ports if ports is not None else tuple(INDUSTRIAL_PORTS)
    if not selected or len(selected) > 64 or len(set(selected)) != len(selected):
        raise ValueError("industrial port list must contain 1..64 distinct entries")
    results = await asyncio.gather(*(tcp_probe(host, port, timeout) for port in selected))
    for item in results:
        item["protocol"] = INDUSTRIAL_PORTS.get(item["port"], "TCP")
    return results


async def scan_cidr(
    cidr: str, timeout: float = 1.0, concurrency: int = 64, max_hosts: int = 1024
) -> dict[str, Any]:
    network = ipaddress.ip_network(cidr, strict=False)
    if not 1 <= max_hosts <= MAX_HOSTS:
        raise ValueError(f"max_hosts must be 1..{MAX_HOSTS}")
    if network.num_addresses > max_hosts:
        raise ValueError(f"network exceeds configured limit of {max_hosts} addresses")
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be 1..{MAX_CONCURRENCY}")
    if not 0 < timeout <= 30:
        raise ValueError("timeout must be in (0, 30]")
    hosts = list(network.hosts())
    semaphore = asyncio.Semaphore(concurrency)

    async def probe(host: ipaddress.IPv4Address | ipaddress.IPv6Address) -> dict[str, Any]:
        async with semaphore:
            started = time.monotonic()
            up = await ping_host(str(host), timeout=timeout)
            return {
                "ip": str(host),
                "status": "UP" if up else "NO_RESPONSE",
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }

    results = await asyncio.gather(*(probe(host) for host in hosts))
    return {
        "network": str(network),
        "scanned": len(results),
        "reachable": sum(item["status"] == "UP" for item in results),
        "results": results,
    }


async def ping_probe(host: str, timeout: float = 1.0) -> dict[str, Any]:
    """Probe one literal host through the shared Core ICMP implementation."""
    ipaddress.ip_address(host)
    result = await core_ping_probe(host, count=1, timeout=timeout)
    return {
        "ip": host,
        "status": "UP" if result.status.value == "PASS" else "NO_RESPONSE",
        "latency_ms": round(float(result.measurements.get("rtt_avg_ms", 0)), 2),
    }
