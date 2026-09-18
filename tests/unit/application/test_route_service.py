"""Unit tests for the RouteService application service.

验证对象：``application/route_service.py`` 通过持有的 Scheduler 委托给
当前路由表——``explain`` 返回正确的 :class:`RouteDecision`，``unmatched_points``
返回未匹配点键；并且热重载替换路由表后（``replace_router``）查询基于最新实例。
纯内存查询，无外部依赖。
"""

from __future__ import annotations

from wind_hub.application.route_service import RouteService
from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig, SchedulerConfig
from wind_hub.domain.engine.pipeline import Pipeline
from wind_hub.domain.engine.router import Router
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.route import RouteRule


def _router(points: list[PointConfig], rules: list[RouteRule]) -> Router:
    return Router(RoutingTable(rules, points))


def _service(router: Router) -> RouteService:
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=router,
        sinks={},
        config=SchedulerConfig(),
    )
    return RouteService(scheduler)


def _points() -> list[PointConfig]:
    return [
        PointConfig(point_id="rotor.speed", device_id="d1", address=PointAddress(type="hr")),
        PointConfig(point_id="no.target", device_id="d1", address=PointAddress(type="hr")),
    ]


def test_explain_routed_point() -> None:
    rules = [RouteRule(name="rotor", match_point_prefix="rotor.", targets=["s1"])]
    decision = _service(_router(_points(), rules)).explain("d1", "rotor.speed")
    assert decision.targets == ["s1"]
    assert decision.source == "rule"
    assert decision.matched_rule == "rotor"


def test_explain_unmatched_point() -> None:
    rules = [RouteRule(name="rotor", match_point_prefix="rotor.", targets=["s1"])]
    decision = _service(_router(_points(), rules)).explain("d1", "no.target")
    assert decision.targets == []
    assert decision.source == "unmatched"


def test_unmatched_points() -> None:
    rules = [RouteRule(name="rotor", match_point_prefix="rotor.", targets=["s1"])]
    assert _service(_router(_points(), rules)).unmatched_points() == [("d1", "no.target")]


async def test_explain_reflects_router_swap_on_hot_reload() -> None:
    """RouteService 持有 Scheduler：热重载替换路由表后查询应基于最新实例。"""
    points = [
        PointConfig(point_id="a.x", device_id="d1", address=PointAddress(type="hr")),
    ]
    old_router = _router(points, [RouteRule(name="old", match_point_prefix="a.", targets=["s1"])])
    scheduler = Scheduler(
        devices={},
        protocols={},
        pipeline=Pipeline([]),
        router=old_router,
        sinks={},
        config=SchedulerConfig(),
    )
    service = RouteService(scheduler)
    assert service.explain("d1", "a.x").targets == ["s1"]

    new_router = _router(points, [RouteRule(name="new", match_point_prefix="a.", targets=["s2"])])
    await scheduler.replace_router(new_router)
    assert service.explain("d1", "a.x").targets == ["s2"]
