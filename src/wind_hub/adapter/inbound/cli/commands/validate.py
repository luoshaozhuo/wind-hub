"""``wind-hub validate`` — validate configuration files without starting the engine."""

from __future__ import annotations

from pathlib import Path

import typer

from wind_hub.adapter.inbound.cli.output import print_error
from wind_hub.config.loader import load_config
from wind_hub.domain.model.errors import WindHubError

app = typer.Typer(name="validate", help="校验配置（schema + 跨文件一致性）")


@app.callback(invoke_without_command=True)
def validate(
    config: Path = typer.Option(
        ..., "--config", help="配置目录（含 system/devices/points/routing.yaml）"
    ),
) -> None:
    """加载并校验配置；成功退出码 0，失败退出码 1。"""
    try:
        cfg = load_config(config)
    except WindHubError as exc:
        print_error(f"配置校验失败：{exc}")
        raise typer.Exit(1) from exc

    devices = len(cfg.devices.devices)
    tables = len(cfg.point_tables.tables)
    points = sum(len(t.points) for t in cfg.point_tables.tables.values())
    sinks = len(cfg.system.sinks)
    rules = len(cfg.routing.rules)
    typer.echo(
        f"配置有效：{devices} 台设备、{tables} 份点表（{points} 个点位）、"
        f"{sinks} 个 sink、{rules} 条路由规则"
    )
