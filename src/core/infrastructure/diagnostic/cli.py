"""Diagnostic CLI：第一阶段仅提供配置检查与快照比较。"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from core.application.diagnostic import DiagnosticResult, DiagnosticStatus
from core.infrastructure.diagnostic.config import check_config, compare_config

app = typer.Typer(help="wind-hub Core diagnostic tools", no_args_is_help=True)
config_app = typer.Typer(help="Validate and compare configuration", no_args_is_help=True)
app.add_typer(config_app, name="config")


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


if __name__ == "__main__":
    app()
