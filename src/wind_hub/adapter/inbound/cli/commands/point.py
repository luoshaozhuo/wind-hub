"""``wind-hub point read`` — read a single point's current value."""

from __future__ import annotations

import asyncio

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_kv
from wind_hub.domain.model.errors import WindHubError
from wind_hub.domain.model.point import PointValue

app = typer.Typer(name="point", help="点位操作")


@app.command()
def read(
    device_id: str = typer.Argument(..., help="设备 ID"),
    point_id: str = typer.Argument(..., help="点位 ID"),
    fresh: bool = typer.Option(False, "--fresh", help="触发一次实时读取（暂忽略）"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """读取单个点位的当前值。"""
    asyncio.run(_read(device_id, point_id, json))


async def _read(device_id: str, point_id: str, as_json: bool) -> None:
    ctx = get_context_or_exit()
    if ctx.query is None:
        print_error("查询用例未配置（AppContext.query 为 None）")
        raise typer.Exit(1)
    try:
        value = await ctx.query.read_point(device_id, point_id)
    except WindHubError as exc:
        if as_json:
            print_json({"error": str(exc)})
        else:
            print_error(f"读取点位失败：{exc}")
        raise typer.Exit(1) from exc

    _print(value, as_json)


def _print(value: PointValue, as_json: bool) -> None:
    if as_json:
        print_json(value.model_dump(mode="json"))
        return
    print_kv(
        {
            "device_id": value.device_id,
            "point_id": value.point_id,
            "value": value.value,
            "quality": value.quality.value,
            "timestamp": value.timestamp,
            "source": value.source,
        }
    )
