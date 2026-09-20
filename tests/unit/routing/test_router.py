"""Unit tests for the Router domain component."""

from __future__ import annotations

from datetime import UTC, datetime

from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.model.route import RouteMatch, RouteRule, RouteTarget
from wind_hub.domain.routing.router import Router

_FROZEN_TS = datetime(2025, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pv(device_id: str, point_id: str, value: float = 1.0) -> PointValue:
    return PointValue(device_id=device_id, point_id=point_id, value=value, timestamp=_FROZEN_TS)


def _make_rule(
    name: str,
    targets: list[str],
    priority: int = 0,
    *,
    all: bool = False,
    device_group: str | None = None,
    point_group: str | None = None,
) -> RouteRule:
    return RouteRule(
        name=name,
        match=RouteMatch(all=all, device_group=device_group, point_group=point_group),
        targets=[RouteTarget(sink=s) for s in targets],
        priority=priority,
    )


def _point(
    point_id: str, sinks: list[str] | None = None, group: str = "default"
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        group=group,
        address=PointAddress(type="hr", register=30001),
        sinks=sinks,
    )


def _table(
    rules: list[RouteRule],
    device_groups: dict[str, str | None] | None = None,
    **device_points: list[PointConfig],
) -> RoutingTable:
    """Build a RoutingTable from ``device_id=[PointConfig, …]`` kwargs."""
    return RoutingTable(
        rules=rules, points_by_device=dict(device_points), device_groups=device_groups
    )


# ---------------------------------------------------------------------------
# 1. Single point, single sink
# ---------------------------------------------------------------------------


class TestBasicRouting:
    def test_single_point_single_sink(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        router = Router(_table(rules, d1=[_point("p1")]))
        batch = [_make_pv("d1", "p1")]
        result = router.route(batch)
        assert result == {"kafka": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 2. Single point, multiple sinks (fan-out)
# ---------------------------------------------------------------------------


class TestFanOut:
    def test_single_point_multiple_sinks(self) -> None:
        rules = [_make_rule("multi", ["kafka", "file"], all=True)]
        router = Router(_table(rules, d1=[_point("p1")]))
        batch = [_make_pv("d1", "p1")]
        result = router.route(batch)
        assert set(result.keys()) == {"kafka", "file"}
        assert len(result["kafka"]) == 1
        assert len(result["file"]) == 1


# ---------------------------------------------------------------------------
# 3. Per-point sink override beats rules
# ---------------------------------------------------------------------------


class TestPointOverride:
    def test_per_point_sinks_override_rules(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        router = Router(_table(rules, d1=[_point("p1", sinks=["override_sink"])]))
        result = router.route([_make_pv("d1", "p1")])
        assert result == {"override_sink": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 4. device_group 匹配
# ---------------------------------------------------------------------------


class TestDeviceGroupMatching:
    def test_match_specific_device_group(self) -> None:
        rules = [
            _make_rule("turbine-only", ["kafka"], device_group="turbine"),
            _make_rule("pcs-only", ["file"], device_group="pcs"),
        ]
        router = Router(
            _table(
                rules,
                device_groups={"d1": "turbine", "d2": "pcs"},
                d1=[_point("p1")],
                d2=[_point("p1")],
            )
        )
        result = router.route([_make_pv("d1", "p1"), _make_pv("d2", "p1")])
        assert "kafka" in result
        assert "file" in result
        assert result["kafka"] == [_make_pv("d1", "p1")]
        assert result["file"] == [_make_pv("d2", "p1")]


# ---------------------------------------------------------------------------
# 5. point_group 匹配
# ---------------------------------------------------------------------------


class TestPointGroupMatching:
    def test_match_point_group(self) -> None:
        rules = [
            _make_rule("fast-rule", ["file"], point_group="fast"),
            _make_rule("slow-rule", ["kafka"], point_group="slow"),
        ]
        router = Router(
            _table(rules, d1=[_point("p1", group="fast"), _point("p2", group="slow")])
        )
        result = router.route([_make_pv("d1", "p1"), _make_pv("d1", "p2")])
        assert result["file"] == [_make_pv("d1", "p1")]
        assert result["kafka"] == [_make_pv("d1", "p2")]


# ---------------------------------------------------------------------------
# 6. Priority-based matching
# ---------------------------------------------------------------------------


class TestPriority:
    def test_higher_priority_wins(self) -> None:
        rules = [
            _make_rule("low-prio", ["file"], priority=0, all=True),
            _make_rule("high-prio", ["kafka"], priority=100, all=True),
        ]
        router = Router(_table(rules, d1=[_point("p1")]))
        result = router.route([_make_pv("d1", "p1")])
        assert result == {"kafka": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 7. Same priority — declaration order wins
# ---------------------------------------------------------------------------


class TestSamePriority:
    def test_declaration_order_wins_on_same_priority(self) -> None:
        # Both rules match everything, same priority — first declared wins
        rules = [
            _make_rule("first", ["kafka"], priority=0, all=True),
            _make_rule("second", ["file"], priority=0, all=True),
        ]
        router = Router(_table(rules, d1=[_point("p1")]))
        result = router.route([_make_pv("d1", "p1")])
        assert result == {"kafka": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 8. Unmatched point — dropped silently
# ---------------------------------------------------------------------------


class TestUnmatched:
    def test_unmatched_point_dropped(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        router = Router(
            _table(
                rules,
                device_groups={"d1": "turbine", "d2": "pcs"},
                d1=[_point("p1")],
                d2=[_point("p2")],
            )
        )
        result = router.route([_make_pv("d1", "p1"), _make_pv("d2", "p2")])
        assert "kafka" in result
        assert len(result["kafka"]) == 1  # only d1/p1
        assert result["kafka"][0].device_id == "d1"


# ---------------------------------------------------------------------------
# 9. Batch routing — mixed devices
# ---------------------------------------------------------------------------


class TestBatchRouting:
    def test_mixed_devices_batch(self) -> None:
        rules = [
            _make_rule("d1-rule", ["kafka"], device_group="turbine"),
            _make_rule("d2-rule", ["file"], device_group="pcs"),
            _make_rule("fast-rule", ["archive"], point_group="fast"),
        ]
        # d1/p-fast → d1-rule matches first (priority 0, name 序在前)
        # d1/p-slow → d1-rule matches first
        # d2/p-fast → d2-rule matches first
        router = Router(
            _table(
                rules,
                device_groups={"d1": "turbine", "d2": "pcs"},
                d1=[_point("p-fast", group="fast"), _point("p-slow", group="slow")],
                d2=[_point("p-fast", group="fast")],
            )
        )
        batch = [
            _make_pv("d1", "p-fast"),
            _make_pv("d1", "p-slow"),
            _make_pv("d2", "p-fast"),
        ]
        result = router.route(batch)
        assert set(result.keys()) == {"kafka", "file"}
        assert len(result["kafka"]) == 2  # d1/p-fast + d1/p-slow
        assert len(result["file"]) == 1  # d2/p-fast


# ---------------------------------------------------------------------------
# 10. explain — returns correct RouteDecision
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_point_override(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        router = Router(_table(rules, d1=[_point("p1", sinks=["override_sink"])]))
        decision = router.explain("d1", "p1")
        assert decision.targets == ["override_sink"]
        assert decision.matched_rule is None
        assert decision.source == "point_override"

    def test_explain_rule_match(self) -> None:
        rules = [_make_rule("my-rule", ["kafka"], device_group="turbine")]
        router = Router(
            _table(rules, device_groups={"d1": "turbine"}, d1=[_point("p1")])
        )
        decision = router.explain("d1", "p1")
        assert decision.targets == ["kafka"]
        assert decision.matched_rule == "my-rule"
        assert decision.source == "rule"

    def test_explain_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        router = Router(_table(rules, device_groups={"d2": "pcs"}, d2=[_point("p2")]))
        decision = router.explain("d2", "p2")
        assert decision.targets == []
        assert decision.matched_rule is None
        assert decision.source == "unmatched"


# ---------------------------------------------------------------------------
# 11. unmatched_points — returns correct list
# ---------------------------------------------------------------------------


class TestUnmatchedPoints:
    def test_unmatched_points_list(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        router = Router(
            _table(
                rules,
                device_groups={"d1": "turbine", "d2": "pcs"},
                d1=[_point("p1")],
                d2=[_point("p2")],
            )
        )
        unmatched = router.unmatched_points()
        assert unmatched == [("d2", "p2")]

    def test_all_matched_no_unmatched(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"], all=True)]
        router = Router(_table(rules, d1=[_point("p1")]))
        assert router.unmatched_points() == []


# ---------------------------------------------------------------------------
# rule_for — 命中规则名查询（Delivery Policy 用）
# ---------------------------------------------------------------------------


class TestRuleFor:
    def test_rule_for_rule_match(self) -> None:
        rules = [_make_rule("my-rule", ["kafka"], device_group="turbine")]
        router = Router(
            _table(rules, device_groups={"d1": "turbine"}, d1=[_point("p1")])
        )
        assert router.rule_for("d1", "p1") == "my-rule"

    def test_rule_for_point_override(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        router = Router(_table(rules, d1=[_point("p1", sinks=["override_sink"])]))
        assert router.rule_for("d1", "p1") is None

    def test_rule_for_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        router = Router(_table(rules, device_groups={"d2": "pcs"}, d2=[_point("p2")]))
        assert router.rule_for("d2", "p2") is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_batch(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        router = Router(_table(rules, d1=[_point("p1")]))
        result = router.route([])
        assert result == {}

    def test_table_size(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"], all=True)]
        table = _table(rules, d1=[_point("p1"), _point("p2")])
        assert table.size == 2
        router = Router(table)
        assert router.table_size == 2
