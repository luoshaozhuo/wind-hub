"""``wind-hub devices`` — list configured devices and their runtime status."""

from __future__ import annotations

import asyncio

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_table

app = typer.Typer(name="devices", help="列出设备")


@app.callback(invoke_without_command=True)
def devices(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """调用 ``QueryUseCase.list_devices()`` 并打印。"""
    asyncio.run(_devices(json))


async def _devices(as_json: bool) -> None:
    ctx = get_context_or_exit()
    if ctx.query is None:
        print_error("查询用例未配置（AppContext.query 为 None）")
        raise typer.Exit(1)
    infos = await ctx.query.list_devices()
    if as_json:
        print_json([info.model_dump(mode="json") for info in infos])
        return
    print_table(
        [
            {
                "device_id": info.device_id,
                "protocol": info.protocol,
                "connected": info.connected,
                "last_seen": info.last_seen,
            }
            for info in infos
        ],
        ["device_id", "protocol", "connected", "last_seen"],
    )
