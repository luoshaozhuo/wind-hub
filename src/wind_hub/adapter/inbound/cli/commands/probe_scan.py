"""``wind-hub probe scan`` — 网段 IP 占用扫描（仅 Linux）。

三层组合（决策 2）：ARP 表 → ICMP ping → TCP connect 兜底，结果合并
去重。默认表格输出，``--json`` / ``--yaml`` 切换机器可读格式
（决策 9）。
"""

from __future__ import annotations

import asyncio

import typer
import yaml

from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_table
from wind_hub.adapter.inbound.cli.probe.scan import scan_network
from wind_hub.adapter.inbound.cli.probe.scan_methods import is_wsl
from wind_hub.adapter.inbound.cli.probe.scan_models import ScanResult, parse_network
from wind_hub.domain.model.errors import WindHubError

# WSL2 提示（决策 0.2）：mirrored 模式从虚拟机内部不可检测，故 WSL 下
# 一律提示。
_WSL_WARNING = (
    "warning: running in WSL2, ARP scan may be ineffective " "(use mirrored mode for full ARP)"
)


def scan(
    network: str = typer.Option(
        ..., "--network", help="网段（CIDR 如 10.0.1.0/24，或范围 10.0.1.1-10.0.1.254）"
    ),
    timeout: float = typer.Option(1.0, "--timeout", help="单 IP 超时（秒）"),
    concurrency: int = typer.Option(128, "--concurrency", help="并发探测数"),
    resolve_hostname: bool = typer.Option(
        False, "--resolve-hostname", help="反查主机名（DNS 反查，慢）"
    ),
    no_icmp: bool = typer.Option(False, "--no-icmp", help="禁用 ICMP ping"),
    no_tcp: bool = typer.Option(False, "--no-tcp", help="禁用 TCP connect 兜底"),
    output_json: bool = typer.Option(False, "--json", help="JSON 输出"),
    output_yaml: bool = typer.Option(False, "--yaml", help="YAML 输出"),
) -> None:
    """网段 IP 占用扫描（仅 Linux），输出占用 IP + MAC 列表。"""
    if output_json and output_yaml:
        print_error("--json 与 --yaml 只能二选一")
        raise typer.Exit(1)
    if is_wsl():
        print_error(_WSL_WARNING)
    try:
        total = len(parse_network(network))
        results = asyncio.run(
            scan_network(
                network,
                timeout=timeout,
                concurrency=concurrency,
                resolve_hostname=resolve_hostname,
                use_icmp=not no_icmp,
                use_tcp=not no_tcp,
            )
        )
    except WindHubError as exc:
        print_error(f"扫描失败：{exc}")
        raise typer.Exit(1) from exc

    if output_json or output_yaml:
        _print_structured(network, total, results, as_json=output_json)
    else:
        _print_table(total, results)


def _print_structured(
    network: str, total: int, results: list[ScanResult], *, as_json: bool
) -> None:
    """JSON / YAML 机器可读输出（决策 9）。"""
    payload = {
        "network": network,
        "total_scanned": total,
        "alive_count": len(results),
        "results": [r.to_dict() for r in results],
    }
    if as_json:
        print_json(payload)
    else:
        typer.echo(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), nl=False)


def _print_table(total: int, results: list[ScanResult]) -> None:
    """默认表格输出 + 汇总行。"""
    rows = [
        {
            "IP": r.ip,
            "MAC": r.mac if r.mac is not None else "-",
            "Hostname": r.hostname if r.hostname is not None else "-",
            "Detected by": ",".join(r.detected_by),
        }
        for r in results
    ]
    print_table(rows, ["IP", "MAC", "Hostname", "Detected by"])
    counts = {
        method: sum(1 for r in results if method in r.detected_by)
        for method in ("arp", "icmp", "tcp")
    }
    typer.echo(
        f"\n共扫描 {total} 个 IP，占用 {len(results)} 个"
        f"（arp: {counts['arp']}, icmp: {counts['icmp']}, tcp: {counts['tcp']}）"
    )
