"""``wind-hub status`` — print a runtime status snapshot."""

from __future__ import annotations

import asyncio

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_table

app = typer.Typer(name="status", help="查看系统运行状态")


@app.callback(invoke_without_command=True)
def status(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """调用 query_service.status() 并打印。"""
    asyncio.run(_status(json))


async def _status(as_json: bool) -> None:
    ctx = get_context_or_exit()
    if ctx.query is None:
        print_error("查询用例未配置（AppContext.query 为 None）")
        raise typer.Exit(1)
    snapshot = await ctx.query.status()
    if as_json:
        print_json(snapshot.model_dump(mode="json"))
        return
    print_table(
        [
            {
                "running": snapshot.running,
                "device_count": snapshot.device_count,
                "sink_count": snapshot.sink_count,
            }
        ],
        ["running", "device_count", "sink_count"],
    )
