"""Bounded monitoring of actual service state and optional authority-owned objects."""

from __future__ import annotations

import asyncio
from typing import Any

import typer

from cli.common_commands import request_common
from cli.service_rpc import invoke

monitor = typer.Typer(help="Read-only service and object monitoring.")

_OBJECT_METHODS = {
    "collector": "GetDevice",
    "gateway": "GetGateway",
    "commander": "GetDevice",
    "sink": "GetDelivery",
    "data-generator": "GetGeneration",
}


def _monitor(
    service: str,
    object_id: str,
    watch: bool,
    interval: float,
    count: int,
) -> None:
    """Poll only completed unary calls; do not infer live device measurements."""
    from cli.main import _run, runtime

    if not 0.1 <= interval <= 60:
        raise typer.BadParameter("--interval must be in [0.1, 60] seconds")
    if count < 0 or count > 100000:
        raise typer.BadParameter("--count must be 0..100000")
    if not watch and count:
        raise typer.BadParameter("--count requires --watch")

    async def one() -> dict[str, Any]:
        if object_id:
            return await invoke(runtime(), service, _OBJECT_METHODS[service], {"id": object_id})
        return await request_common(service, "GetServiceStatus", {})

    async def task() -> dict[str, Any]:
        if not watch:
            return await one()
        iterations = 0
        while count == 0 or iterations < count:
            observation = await one()
            runtime().present(f"monitor {service}", observation)
            iterations += 1
            if count == 0 or iterations < count:
                await asyncio.sleep(interval)
        return {"service": service, "observations": iterations, "complete": True}

    _run(f"monitor {service}", task())


@monitor.command("collector")
def monitor_collector(
    object_id: str = "",
    watch: bool = False,
    interval: float = 1.0,
    count: int = 0,
) -> None:
    _monitor("collector", object_id, watch, interval, count)


@monitor.command("gateway")
def monitor_gateway(
    object_id: str = "",
    watch: bool = False,
    interval: float = 1.0,
    count: int = 0,
) -> None:
    _monitor("gateway", object_id, watch, interval, count)


@monitor.command("commander")
def monitor_commander(
    object_id: str = "",
    watch: bool = False,
    interval: float = 1.0,
    count: int = 0,
) -> None:
    _monitor("commander", object_id, watch, interval, count)


@monitor.command("sink")
def monitor_sink(
    object_id: str = "",
    watch: bool = False,
    interval: float = 1.0,
    count: int = 0,
) -> None:
    _monitor("sink", object_id, watch, interval, count)


@monitor.command("data-generator")
def monitor_data_generator(
    object_id: str = "",
    watch: bool = False,
    interval: float = 1.0,
    count: int = 0,
) -> None:
    _monitor("data-generator", object_id, watch, interval, count)
