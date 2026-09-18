"""``wind-hub reload`` — hot-reload configuration via the config service."""

from __future__ import annotations

import asyncio

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json
from wind_hub.domain.model.errors import WindHubError

app = typer.Typer(name="reload", help="热加载配置（无需重启）")


@app.callback(invoke_without_command=True)
def reload(
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """调用 config_service.reload() 并打印结果。"""
    asyncio.run(_reload(json))


async def _reload(as_json: bool) -> None:
    ctx = get_context_or_exit()
    if ctx.config_service is None:
        print_error("配置服务未配置（AppContext.config_service 为 None）")
        raise typer.Exit(1)
    try:
        result = await ctx.config_service.reload()
    except WindHubError as exc:
        if as_json:
            print_json({"success": False, "error": str(exc)})
        else:
            print_error(f"配置热加载失败：{exc}")
        raise typer.Exit(1) from exc

    if not result.success:
        details = "; ".join(result.errors) if result.errors else "unknown error"
        if as_json:
            print_json({"success": False, "error": details})
        else:
            print_error(f"配置热加载失败：{details}")
        raise typer.Exit(1)

    if as_json:
        print_json({"success": True})
    else:
        typer.echo("配置热加载成功")
