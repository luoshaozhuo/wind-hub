"""``wind-hub cmd send`` — issue a single write command and block for the result."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_kv
from wind_hub.domain.model.command import Command
from wind_hub.domain.model.errors import WindHubError

app = typer.Typer(name="cmd", help="指令操作")


@app.command()
def send(
    device_id: str = typer.Argument(..., help="设备 ID"),
    point_id: str = typer.Argument(..., help="点位 ID"),
    value: str = typer.Argument(..., help="要写入的值（按 int/float/bool/str 依次尝试解析）"),
    timeout: float = typer.Option(5.0, "--timeout", help="写入超时（秒）"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """同步发送写指令并等待结果；成功退出码 0，失败退出码 1。"""
    asyncio.run(_send(device_id, point_id, value, timeout, json))


async def _send(device_id: str, point_id: str, value: str, timeout: float, as_json: bool) -> None:
    ctx = get_context_or_exit()
    if ctx.command_service is None:
        print_error("指令服务未配置（AppContext.command_service 为 None）")
        raise typer.Exit(1)
    command = Command(
        command_id=str(uuid.uuid4()),
        device_id=device_id,
        point_id=point_id,
        value=_coerce_value(value),
        timeout=timeout,
    )
    try:
        result = await ctx.command_service.send(command)
    except WindHubError as exc:
        if as_json:
            print_json({"success": False, "error": str(exc)})
        else:
            print_error(f"指令发送失败：{exc}")
        raise typer.Exit(1) from exc

    if as_json:
        print_json(result.model_dump(mode="json"))
    else:
        print_kv(
            {
                "command_id": result.command_id,
                "success": result.success,
                "error": result.error,
                "finished_at": result.finished_at,
            }
        )
    if not result.success:
        raise typer.Exit(1)


def _coerce_value(raw: str) -> Any:
    """Coerce a CLI string into int/float/bool/str, in that order."""
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw
