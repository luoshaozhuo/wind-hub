"""Read-only CLI diagnostics for Sink Service targets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from core.application.diagnostic import DiagnosticResult, DiagnosticStatus
from sink_service.infrastructure.diagnostic import benchmark_sink, check_sink

app = typer.Typer(help="Sink Service diagnostics", no_args_is_help=True)


def _display(result: DiagnosticResult, json_output: bool) -> None:
    """Display a structured diagnostic without changing target data."""
    if json_output:
        typer.echo(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        typer.echo(f"{result.category}: {result.target}: {result.status.value}")
        for item in result.findings:
            typer.echo(f"{item.code}: {item.message}")
    if result.status is DiagnosticStatus.FAIL:
        raise typer.Exit(code=1)
    if result.status in {DiagnosticStatus.UNKNOWN, DiagnosticStatus.SKIPPED}:
        raise typer.Exit(code=3)


@app.command("check")
def check(
    sink_id: str = typer.Option(..., "--sink"),
    config: Path = typer.Option(..., "--config"),
    timeout: float = typer.Option(5.0, min=0.1, max=30),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Check downstream application health without writing data."""
    _display(asyncio.run(check_sink(config, sink_id, timeout=timeout)), json_output)


@app.command("benchmark")
def benchmark(
    sink_id: str = typer.Option(..., "--sink"),
    config: Path = typer.Option(..., "--config"),
    samples: int = typer.Option(10, min=1, max=200),
    interval: float = typer.Option(0.2, min=0, max=5),
    timeout: float = typer.Option(5.0, min=0.1, max=30),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Benchmark read-only Sink diagnostics."""
    _display(
        asyncio.run(
            benchmark_sink(
                config,
                sink_id,
                samples=samples,
                interval=interval,
                timeout=timeout,
            )
        ),
        json_output,
    )


def main() -> None:
    """Execute diagnostic CLI."""
    app()


if __name__ == "__main__":
    main()
