"""RouteQueryUseCase 的单元测试。

验证对象：``application/usecase/route_query.py``——经 Runtime 的
``current_router`` 委托给当前路由表：``explain`` 返回正确的
:class:`RouteDecision`，``unmatched_points`` 返回未匹配点键；热重载替换
路由表后（``Runtime.replace_router``）查询基于最新实例。

纯内存查询，无外部依赖。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from wind_hub.application.port.scheduling import SchedulerPort
from wind_hub.application.runtime import Runtime
from wind_hub.application.usecase.route_query import RouteQueryUseCase
from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig, SchedulerConfig
from wind_hub.domain.acquisition import AcquisitionEngine
from wind_hub.domain.command import Dispatcher
from wind_hub.domain.model.route import RouteMatch, RouteRule, RouteTarget
from wind_hub.domain.processing import Pipeline
from wind_hub.domain.routing import Router


def _router(points: list[PointConfig], rules: list[RouteRule]) -> Router:
    return Router(RoutingTable(rules, {"d1": points}))


def _runtime(router: Router) -> Runtime:
    engine = AcquisitionEngine(protocols={}, pipeline=Pipeline([]), router=router)
    return Runtime(
        devices={},
        protocols={},
        sinks={},
        engine=engine,
        scheduler=MagicMock(spec=SchedulerPort),
        dispatcher=MagicMock(spec=Dispatcher),
        config=SchedulerConfig(),
    )


def _points() -> list[PointConfig]:
    return [
        PointConfig(point_id="rotor.speed", group="fast", address=PointAddress(type="hr")),
        PointConfig(point_id="no.target", group="other", address=PointAddress(type="hr")),
    ]


def test_explain_routed_point() -> None:
    rules = [
        RouteRule(
            name="rotor",
            match=RouteMatch(point_group="fast"),
            targets=[RouteTarget(sink="s1")],
        )
    ]
    usecase = RouteQueryUseCase(_runtime(_router(_points(), rules)))
    decision = usecase.explain("d1", "rotor.speed")
    assert decision.targets == ["s1"]
    assert decision.source == "rule"
    assert decision.matched_rule == "rotor"


def test_explain_unmatched_point() -> None:
    rules = [
        RouteRule(
            name="rotor",
            match=RouteMatch(point_group="fast"),
            targets=[RouteTarget(sink="s1")],
        )
    ]
    usecase = RouteQueryUseCase(_runtime(_router(_points(), rules)))
    decision = usecase.explain("d1", "no.target")
    assert decision.targets == []
    assert decision.source == "unmatched"


def test_unmatched_points() -> None:
    rules = [
        RouteRule(
            name="rotor",
            match=RouteMatch(point_group="fast"),
            targets=[RouteTarget(sink="s1")],
        )
    ]
    usecase = RouteQueryUseCase(_runtime(_router(_points(), rules)))
    assert usecase.unmatched_points() == [("d1", "no.target")]


async def test_explain_reflects_router_swap_on_hot_reload() -> None:
    """经 Runtime.current_router 查询：热重载替换路由表后基于最新实例。"""
    points = [
        PointConfig(point_id="a.x", group="g", address=PointAddress(type="hr")),
    ]
    old_router = _router(
        points,
        [
            RouteRule(
                name="old",
                match=RouteMatch(point_group="g"),
                targets=[RouteTarget(sink="s1")],
            )
        ],
    )
    runtime = _runtime(old_router)
    usecase = RouteQueryUseCase(runtime)
    assert usecase.explain("d1", "a.x").targets == ["s1"]

    new_router = _router(
        points,
        [
            RouteRule(
                name="new",
                match=RouteMatch(point_group="g"),
                targets=[RouteTarget(sink="s2")],
            )
        ],
    )
    await runtime.replace_router(new_router)
    assert usecase.explain("d1", "a.x").targets == ["s2"]
