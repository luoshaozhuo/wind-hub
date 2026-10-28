"""Diagnostic CLI：配置、网络及防火墙诊断。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from core.application.diagnostic import DiagnosticResult, DiagnosticStatus
from core.infrastructure.diagnostic.config import check_config, compare_config
from core.infrastructure.diagnostic import firewall, network

app = typer.Typer(help="wind-hub Core diagnostic tools", no_args_is_help=True)
config_app = typer.Typer(help="Validate and compare configuration", no_args_is_help=True)
app.add_typer(config_app, name="config")
network_app = typer.Typer(help="Inspect Linux networking", no_args_is_help=True)
app.add_typer(network_app, name="network")
firewall_app = typer.Typer(help="Inspect local firewall (read-only)", no_args_is_help=True)
network_app.add_typer(firewall_app, name="firewall")


def _output(result: DiagnosticResult, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        typer.echo(f"{result.category}: {result.target}: {result.status.value}")
        for finding in result.findings:
            location = f" [{finding.location}]" if finding.location else ""
            typer.echo(f"  {finding.code}{location}: {finding.message}")
        for name, value in result.measurements.items():
            typer.echo(f"  {name}: {value}")
        if result.evidence:
            typer.echo(json.dumps(result.evidence, ensure_ascii=False, indent=2))
    if result.status is DiagnosticStatus.FAIL:
        raise typer.Exit(code=1)
    if result.status in {DiagnosticStatus.UNKNOWN, DiagnosticStatus.SKIPPED}:
        raise typer.Exit(code=3)


@config_app.command("check")
def check(
    directory: Path = typer.Argument(..., help="Configuration directory"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
) -> None:
    """校验配置目录及跨文件引用，不连接外部系统。"""
    _output(check_config(directory), json_output)


@config_app.command("diff")
def diff(
    previous: Path = typer.Argument(..., help="Previous configuration directory"),
    current: Path = typer.Argument(..., help="Current configuration directory"),
    json_output: bool = typer.Option(False, "--json", help="Print JSON"),
) -> None:
    """对比两套已经通过校验的配置快照。"""
    _output(compare_config(previous, current), json_output)



@network_app.command("interfaces")
def network_interfaces(json_output: bool = typer.Option(False, "--json")) -> None:
    _output(asyncio.run(network.interfaces()), json_output)


@network_app.command("routes")
def network_routes(json_output: bool = typer.Option(False, "--json")) -> None:
    _output(asyncio.run(network.routes()), json_output)


@network_app.command("ports")
def network_ports(
    listening: bool = typer.Option(False, "--listening"),
    established: bool = typer.Option(False, "--established"),
    port: int | None = typer.Option(None, "--port", min=1, max=65535),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _output(asyncio.run(network.ports(
        listening=listening, established=established, port=port,
    )), json_output)


@network_app.command("port")
def network_port(
    port: int = typer.Argument(..., min=1, max=65535),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _output(asyncio.run(network.ports(port=port)), json_output)


@network_app.command("ping")
def network_ping(
    host: str,
    count: int = typer.Option(4, min=1, max=20),
    timeout: float = typer.Option(1.0, min=0.1, max=30),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _output(asyncio.run(network.ping_probe(host, count=count, timeout=timeout)), json_output)


@network_app.command("tcp")
def network_tcp(
    host: str,
    port: int = typer.Argument(..., min=1, max=65535),
    timeout: float = typer.Option(1.0, min=0.1, max=30),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    _output(asyncio.run(network.tcp_probe(host, port, timeout=timeout)), json_output)


@network_app.command("scan")
def network_scan(
    host: str,
    ports: str = typer.Option(..., "--ports", help="Comma-separated ports, max 128"),
    timeout: float = typer.Option(1.0, min=0.1, max=30),
    concurrency: int = typer.Option(8, min=1, max=16),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    try:
        numbers = [int(value.strip()) for value in ports.split(",")]
        if not numbers or len(numbers) > 128 or any(not 1 <= n <= 65535 for n in numbers):
            raise ValueError("ports must contain 1..128 valid TCP ports")
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--ports") from exc
    _output(asyncio.run(network.scan_ports(
        host, numbers, timeout=timeout, concurrency=concurrency,
    )), json_output)



@network_app.command("bandwidth")
def network_bandwidth(
    host: str,
    duration: int = typer.Option(5, min=1, max=10),
    port: int = typer.Option(5201, min=1, max=65535),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """显式执行限速 iperf3 TCP 测速；要求目标端有 iperf3 Server。"""
    _output(asyncio.run(network.bandwidth(host, duration=duration, port=port)), json_output)


@firewall_app.command("status")
def firewall_status(json_output: bool = typer.Option(False, "--json")) -> None:
    _output(asyncio.run(firewall.firewall_status()), json_output)


@firewall_app.command("rules")
def firewall_rules(json_output: bool = typer.Option(False, "--json")) -> None:
    _output(asyncio.run(firewall.firewall_status()), json_output)


@firewall_app.command("check")
def firewall_check(
    port: int = typer.Option(..., "--port", min=1, max=65535),
    direction: str = typer.Option("inbound", "--direction"),
    protocol: str = typer.Option("tcp", "--protocol"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    if direction not in {"inbound", "outbound"} or protocol not in {"tcp", "udp"}:
        raise typer.BadParameter("direction must be inbound/outbound; protocol tcp/udp")
    _output(asyncio.run(firewall.firewall_check(
        port, direction=direction, protocol=protocol,
    )), json_output)


if __name__ == "__main__":
    app()
