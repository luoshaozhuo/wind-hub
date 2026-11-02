"""Local one-shot ICMP and TCP probes with strict concurrency bounds."""

from __future__ import annotations

import asyncio
import ipaddress
import platform
import time
from typing import Any

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
    if not 1 <= port <= 65535:
        raise ValueError("port must be in 1..65535")
    if not 0 < timeout <= 30:
        raise ValueError("timeout must be in (0, 30]")
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout)
        status = "OPEN"
    except TimeoutError:
        status = "TIMEOUT"
    except OSError:
        status = "CLOSED"
    return {
        "host": host,
        "port": port,
        "tcp_state": status,
        "latency_ms": round((time.monotonic() - start) * 1000, 2),
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
    option = "-n" if platform.system() == "Windows" else "-c"
    wait_option = "-w" if platform.system() == "Windows" else "-W"
    wait_value = (
        str(int(timeout * 1000)) if platform.system() == "Windows" else str(max(1, int(timeout)))
    )

    async def probe(host: ipaddress.IPv4Address | ipaddress.IPv6Address) -> dict[str, Any]:
        async with semaphore:
            started = time.monotonic()
            try:
                process = await asyncio.create_subprocess_exec(
                    "ping",
                    option,
                    "1",
                    wait_option,
                    wait_value,
                    str(host),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout + 1.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                except asyncio.CancelledError:
                    process.kill()
                    await process.wait()
                    raise
                status = "UP" if process.returncode == 0 else "NO_RESPONSE"
            except FileNotFoundError as exc:
                raise RuntimeError("system ping executable not found") from exc
            return {
                "ip": str(host),
                "status": status,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }

    results = await asyncio.gather(
        *(
            probe(host)
            for host in hosts
            if isinstance(host, ipaddress.IPv4Address | ipaddress.IPv6Address)
        )
    )
    return {
        "network": str(network),
        "scanned": len(results),
        "reachable": sum(item["status"] == "UP" for item in results),
        "results": results,
    }


async def ping_probe(host: str, timeout: float = 1.0) -> dict[str, Any]:
    """Run a single bounded ICMP probe against a validated IP literal."""
    address = ipaddress.ip_address(host)
    prefix = 32 if address.version == 4 else 128
    outcome = await scan_cidr(
        f"{address}/{prefix}",
        timeout=timeout,
        concurrency=1,
        max_hosts=1,
    )
    if len(outcome["results"]) != 1:
        raise RuntimeError("unexpected ping result count")
    result: dict[str, Any] = outcome["results"][0]
    return result
