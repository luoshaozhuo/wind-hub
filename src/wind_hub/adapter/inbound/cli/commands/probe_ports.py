"""``wind-hub probe ports`` — 指定 IP 的 TCP 端口扫描（仅 Linux）。

``--ports`` 未指定时扫描端口配置的 ``mapping`` 全部端口（默认
``configs/ports.yaml``，缺失时回落内置工业协议映射，决策 2）；
默认表格输出，``--json`` / ``--yaml`` 切换机器可读格式（决策 8）。
"""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path

import typer
import yaml

from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_table
from wind_hub.adapter.inbound.cli.probe.ports import scan_ports
from wind_hub.adapter.inbound.cli.probe.ports_models import PortScanResult, PortState
from wind_hub.adapter.inbound.cli.probe.ports_parse import default_ports, parse_ports
from wind_hub.config.ports_config import DEFAULT_PORTS_CONFIG_PATH, load_ports_config
from wind_hub.domain.model.errors import ConfigError, WindHubError


def ports(
    host: str = typer.Option(..., "--host", help="目标 IP（如 10.0.1.1）"),
    ports_spec: str | None = typer.Option(
        None, "--ports", help="端口列表（如 502,2404,8000-8100）；缺省用端口配置的默认端口集"
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="单端口超时（秒）；缺省用端口配置的 timeout"
    ),
    concurrency: int | None = typer.Option(
        None, "--concurrency", help="并发探测数；缺省用端口配置的 concurrency"
    ),
    ports_config: Path = typer.Option(
        DEFAULT_PORTS_CONFIG_PATH,
        "--ports-config",
        help="端口扫描配置文件（缺省 configs/ports.yaml；文件不存在时用内置默认值）",
    ),
    output_json: bool = typer.Option(False, "--json", help="JSON 输出"),
    output_yaml: bool = typer.Option(False, "--yaml", help="YAML 输出"),
) -> None:
    """指定 IP 端口扫描（仅 Linux），区分 open / closed / timeout 三态。"""
    if output_json and output_yaml:
        print_error("--json 与 --yaml 只能二选一")
        raise typer.Exit(1)
    try:
        _validate_host(host)
        config = load_ports_config(ports_config)
        port_list = parse_ports(ports_spec) if ports_spec is not None else default_ports(config)
        result = asyncio.run(
            scan_ports(
                host,
                port_list,
                timeout=timeout if timeout is not None else config.timeout,
                concurrency=concurrency if concurrency is not None else config.concurrency,
                config=config,
            )
        )
    except WindHubError as exc:
        print_error(f"端口扫描失败：{exc}")
        raise typer.Exit(1) from exc

    if output_json or output_yaml:
        _print_structured(result, as_json=output_json)
    else:
        _print_table(result)


def _validate_host(host: str) -> None:
    """校验目标是合法 IPv4 地址（主机名解析属于 diagnose 的职责）。"""
    try:
        ipaddress.IPv4Address(host)
    except ValueError as exc:
        raise ConfigError(f"invalid target IP '{host}'") from exc


def _print_structured(result: PortScanResult, *, as_json: bool) -> None:
    """JSON / YAML 机器可读输出（决策 8）。"""
    if as_json:
        print_json(result.to_dict())
    else:
        typer.echo(yaml.safe_dump(result.to_dict(), allow_unicode=True, sort_keys=False), nl=False)


def _print_table(result: PortScanResult) -> None:
    """默认表格输出 + 汇总行。"""
    rows = [
        {
            "Port": p.port,
            "State": p.state.value,
            "Service": p.service_guess if p.service_guess is not None else "-",
        }
        # open 排前面便于现场快速定位（同态内按端口号升序）
        for p in sorted(result.ports, key=lambda p: (p.state is not PortState.OPEN, p.port))
    ]
    print_table(rows, ["Port", "State", "Service"])
    typer.echo(f"\n共扫描 {result.total} 个端口，开放 {result.open_count} 个")
