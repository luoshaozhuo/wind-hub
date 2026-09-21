"""``wind-hub route explain`` — explain the routing decision for a point."""

from __future__ import annotations

import typer

from wind_hub.adapter.inbound.cli.context import get_context_or_exit
from wind_hub.adapter.inbound.cli.output import print_error, print_json, print_kv
from wind_hub.domain.model.route import RouteDecision

app = typer.Typer(name="route", help="路由操作")


@app.command()
def explain(
    device_id: str = typer.Argument(..., help="设备 ID"),
    point_id: str = typer.Argument(..., help="点位 ID"),
    json: bool = typer.Option(False, "--json", help="输出 JSON 格式"),
) -> None:
    """打印某点位的路由决策（目标 sink、命中规则、来源）。"""
    ctx = get_context_or_exit()
    if ctx.route_query is None:
        print_error("路由查询用例未配置（AppContext.route_query 为 None）")
        raise typer.Exit(1)

    decision: RouteDecision = ctx.route_query.explain(device_id, point_id)
    if json:
        print_json(decision.model_dump(mode="json"))
    else:
        print_kv(
            {
                "device_id": decision.device_id,
                "point_id": decision.point_id,
                "targets": decision.targets,
                "matched_rule": decision.matched_rule,
                "source": decision.source,
            }
        )
