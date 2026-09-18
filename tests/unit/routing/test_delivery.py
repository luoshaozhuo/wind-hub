"""DeliveryDispatcher（``domain/routing/delivery.py``）的单元测试。

验证对象：投递策略层——Router 决定「发到哪些 sink」之后，
DeliveryDispatcher 按 **RouteTarget** 上的 ``DeliveryConfig`` 决定「这批
是否投递」。策略键空间：

- ``interval`` / ``every_n``：``(规则名, sink名)``——同一规则下不同
  sink 的节拍相互独立；
- ``on_change``：``(sink, 设备, 点)``；
- 点位级 sinks 覆盖（point_override）不属任何规则 → 恒按 always。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.model.route import DeliveryConfig, RouteRule, RouteTarget
from wind_hub.domain.routing import DeliveryDispatcher, Router, policies_from_rules


def _pv(point_id: str = "p1", value: float = 1.0, device_id: str = "d1") -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=value)


def _point(point_id: str, sinks: list[str] | None = None) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        address=PointAddress(type="holding_register"),
        sinks=sinks,
    )


def _dispatcher(
    rules: list[RouteRule],
    points: list[PointConfig] | None = None,
    clock: object = None,
) -> DeliveryDispatcher:
    """单设备 d1 的便捷构造；``clock`` 可传假时钟（``lambda: t``）。"""
    pts = points if points is not None else [_point("p1")]
    table = RoutingTable(rules, {"d1": pts})
    router = Router(table)
    kwargs = {"clock": clock} if clock is not None else {}
    return DeliveryDispatcher(router, policies_from_rules(rules), **kwargs)  # type: ignore[arg-type]


def _rule(name: str, *targets: RouteTarget) -> RouteRule:
    return RouteRule(name=name, targets=list(targets) or [RouteTarget(sink="s1")])


def _target(sink: str, delivery: DeliveryConfig | None = None) -> RouteTarget:
    return RouteTarget(sink=sink, delivery=delivery)


# ---------------------------------------------------------------------------
# DeliveryConfig 参数校验 / policies_from_rules
# ---------------------------------------------------------------------------


class TestDeliveryConfigValidation:
    def test_default_is_always(self) -> None:
        assert DeliveryConfig().type == "always"

    def test_interval_requires_positive_interval(self) -> None:
        with pytest.raises(ValidationError, match="interval"):
            DeliveryConfig(type="interval")
        with pytest.raises(ValidationError, match="interval"):
            DeliveryConfig(type="interval", interval=0.0)

    def test_every_n_requires_n_at_least_1(self) -> None:
        with pytest.raises(ValidationError, match="n"):
            DeliveryConfig(type="every_n")
        with pytest.raises(ValidationError, match="n"):
            DeliveryConfig(type="every_n", n=0)

    def test_policies_from_rules_keyed_by_rule_and_sink(self) -> None:
        """策略属于 target：{(规则, sink): policy}；无 delivery 的 target 不出现。"""
        rules = [
            _rule(
                "r1",
                _target("s1"),
                _target("s2", DeliveryConfig(type="every_n", n=2)),
            ),
            _rule("r2", _target("s1", DeliveryConfig(type="on_change"))),
        ]
        policies = policies_from_rules(rules)
        assert set(policies) == {("r1", "s2"), ("r2", "s1")}


# ---------------------------------------------------------------------------
# always
# ---------------------------------------------------------------------------


class TestAlways:
    def test_no_delivery_target_passes_every_batch(self) -> None:
        d = _dispatcher([_rule("r1")])
        routed = {"s1": [_pv()]}
        for _ in range(3):
            assert d.evaluate(routed) == routed

    def test_explicit_always_passes_every_batch(self) -> None:
        d = _dispatcher([_rule("r1", _target("s1", DeliveryConfig(type="always")))])
        routed = {"s1": [_pv()]}
        assert d.evaluate(routed) == routed
        assert d.evaluate(routed) == routed


# ---------------------------------------------------------------------------
# interval
# ---------------------------------------------------------------------------


class TestInterval:
    def test_first_batch_passes_then_suppressed_until_interval_elapses(self) -> None:
        now = 100.0
        d = _dispatcher(
            [_rule("r1", _target("s1", DeliveryConfig(type="interval", interval=10.0)))],
            clock=lambda: now,
        )
        routed = {"s1": [_pv()]}
        assert d.evaluate(routed) == routed  # 首批立即投递

        now += 5.0  # 距上次 5s < 10s → 整批抑制
        assert d.evaluate(routed) == {}

        now += 5.0  # 距上次 10s → 放行
        assert d.evaluate(routed) == routed

    def test_gate_is_per_rule_sink_pair(self) -> None:
        """同一规则下两个 sink 都声明 interval——节拍状态按 (规则, sink) 隔离。"""
        now = 0.0
        rules = [
            _rule(
                "r1",
                _target("s1", DeliveryConfig(type="interval", interval=10.0)),
                _target("s2", DeliveryConfig(type="interval", interval=10.0)),
            )
        ]
        d = _dispatcher(rules, clock=lambda: now)
        # s1 已投递过（通过一次只含 s1 的评估）
        d.evaluate({"s1": [_pv()]})
        now += 5.0
        # s1 被抑制，s2 首次投递 → 放行
        v = _pv()
        assert d.evaluate({"s1": [v], "s2": [v]}) == {"s2": [v]}


# ---------------------------------------------------------------------------
# every_n
# ---------------------------------------------------------------------------


class TestEveryN:
    def test_first_batch_passes_then_every_nth(self) -> None:
        d = _dispatcher([_rule("r1", _target("s1", DeliveryConfig(type="every_n", n=3)))])
        routed = {"s1": [_pv()]}
        results = [bool(d.evaluate(routed)) for _ in range(7)]
        # 第 1、4、7 批投递（首批即投，之后每 3 批一次）
        assert results == [True, False, False, True, False, False, True]


# ---------------------------------------------------------------------------
# 同 rule 多 sink 不同 policy —— 三个 sink 行为独立
# ---------------------------------------------------------------------------


class TestFanoutIndependence:
    def test_kafka_always_db_interval_file_every_n(self) -> None:
        """核心场景：同一点经同一规则扇出到三个 sink——
        Kafka 每批都投、DB 按 10s 限流、File 每 3 批投一次。"""
        now = 0.0
        rules = [
            _rule(
                "fanout",
                _target("kafka", DeliveryConfig(type="always")),
                _target("db", DeliveryConfig(type="interval", interval=10.0)),
                _target("file", DeliveryConfig(type="every_n", n=3)),
            )
        ]
        d = _dispatcher(rules, clock=lambda: now)

        def sinks_at(t: float) -> set[str]:
            nonlocal now
            now = t
            routed = {"kafka": [_pv()], "db": [_pv()], "file": [_pv()]}
            return set(d.evaluate(routed))

        # t=0：首批——三个 sink 都投递
        assert sinks_at(0.0) == {"kafka", "db", "file"}
        # t=1：kafka always；db 距上次 1s < 10s 抑制；file 第 2 批抑制
        assert sinks_at(1.0) == {"kafka"}
        # t=2：file 第 3 批仍抑制（count=2 非 3 的倍数）
        assert sinks_at(2.0) == {"kafka"}
        # t=5：file 第 4 批（count=3）放行；db 距上次 5s 抑制
        assert sinks_at(5.0) == {"kafka", "file"}
        # t=10：db 间隔到 → 放行；file 第 5 批抑制
        assert sinks_at(10.0) == {"kafka", "db"}


# ---------------------------------------------------------------------------
# on_change
# ---------------------------------------------------------------------------


class TestOnChange:
    def test_first_seen_passes_unchanged_suppressed_changed_passes(self) -> None:
        d = _dispatcher([_rule("r1", _target("s1", DeliveryConfig(type="on_change")))])
        v1, v2, v3 = _pv(value=1.0), _pv(value=1.0), _pv(value=2.0)
        assert d.evaluate({"s1": [v1]}) == {"s1": [v1]}
        assert d.evaluate({"s1": [v2]}) == {}  # 值未变 → 抑制
        assert d.evaluate({"s1": [v3]}) == {"s1": [v3]}

    def test_state_is_per_sink_device_point(self) -> None:
        """不同点/不同 sink 的变化记忆相互独立。"""
        rules = [
            _rule(
                "r1",
                _target("s1", DeliveryConfig(type="on_change")),
                _target("s2", DeliveryConfig(type="on_change")),
            )
        ]
        pts = [_point("p1"), _point("p2")]
        d = _dispatcher(rules, points=pts)
        d.evaluate({"s1": [_pv("p1", 1.0), _pv("p2", 5.0)], "s2": [_pv("p1", 1.0)]})
        # p1@s1 未变（抑制）；p1@s2 未变（抑制）
        assert d.evaluate({"s1": [_pv("p1", 1.0)], "s2": [_pv("p1", 1.0)]}) == {}
        # p2 变化 → 仅 p2 投递
        v = _pv("p2", 6.0)
        assert d.evaluate({"s1": [v]}) == {"s1": [v]}


# ---------------------------------------------------------------------------
# 点位级覆盖 → always
# ---------------------------------------------------------------------------


class TestPointOverride:
    def test_overridden_point_bypasses_target_policy(self) -> None:
        """声明了点位级 sinks 的点不属任何规则——即使存在 interval target 也
        恒按 always 投递。"""
        rules = [_rule("r1", _target("s1", DeliveryConfig(type="interval", interval=1000.0)))]
        pts = [_point("p1", sinks=["s1"])]  # 点位级覆盖
        d = _dispatcher(rules, points=pts)
        routed = {"s1": [_pv()]}
        assert d.evaluate(routed) == routed
        assert d.evaluate(routed) == routed  # 第二批仍投递（无 interval 抑制）
