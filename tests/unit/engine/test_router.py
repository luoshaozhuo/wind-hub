"""Unit tests for the Router domain component."""

from __future__ import annotations

from datetime import UTC, datetime

from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.engine.router import Router
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.model.route import RouteRule

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
    match_device: str | None = None,
    match_point_prefix: str | None = None,
) -> RouteRule:
    return RouteRule(
        name=name,
        targets=targets,
        priority=priority,
        match_device=match_device,
        match_point_prefix=match_point_prefix,
    )


# ---------------------------------------------------------------------------
# 1. Single point, single sink
# ---------------------------------------------------------------------------


class TestBasicRouting:
    def test_single_point_single_sink(self) -> None:
        rules = [_make_rule("default", ["kafka"], match_point_prefix=None)]
        points = [
            PointConfig(
                point_id="p1",
                device_id="d1",
                address=PointAddress(type="hr", register=30001),
            )
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        batch = [_make_pv("d1", "p1")]
        result = router.route(batch)
        assert result == {"kafka": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 2. Single point, multiple sinks (fan-out)
# ---------------------------------------------------------------------------


class TestFanOut:
    def test_single_point_multiple_sinks(self) -> None:
        rules = [_make_rule("multi", ["kafka", "file"])]
        points = [
            PointConfig(
                point_id="p1",
                device_id="d1",
                address=PointAddress(type="hr", register=30001),
            )
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
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
        rules = [_make_rule("default", ["kafka"])]
        points = [
            PointConfig(
                point_id="p1",
                device_id="d1",
                address=PointAddress(type="hr", register=30001),
                sinks=["override_sink"],
            )
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        result = router.route([_make_pv("d1", "p1")])
        assert result == {"override_sink": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 4. Device-level matching
# ---------------------------------------------------------------------------


class TestDeviceMatching:
    def test_match_specific_device(self) -> None:
        rules = [
            _make_rule("d1-only", ["kafka"], match_device="d1"),
            _make_rule("d2-only", ["file"], match_device="d2"),
        ]
        points = [
            PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr")),
            PointConfig(point_id="p1", device_id="d2", address=PointAddress(type="hr")),
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        result = router.route([_make_pv("d1", "p1"), _make_pv("d2", "p1")])
        assert "kafka" in result
        assert "file" in result
        assert result["kafka"] == [_make_pv("d1", "p1")]
        assert result["file"] == [_make_pv("d2", "p1")]


# ---------------------------------------------------------------------------
# 5. Point-prefix matching
# ---------------------------------------------------------------------------


class TestPrefixMatching:
    def test_match_point_prefix(self) -> None:
        rules = [
            _make_rule("rotor-rule", ["file"], match_point_prefix="rotor."),
            _make_rule("gen-rule", ["kafka"], match_point_prefix="gen."),
        ]
        points = [
            PointConfig(point_id="rotor.speed", device_id="d1", address=PointAddress(type="hr")),
            PointConfig(point_id="gen.power", device_id="d1", address=PointAddress(type="hr")),
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        result = router.route([_make_pv("d1", "rotor.speed"), _make_pv("d1", "gen.power")])
        assert result["file"] == [_make_pv("d1", "rotor.speed")]
        assert result["kafka"] == [_make_pv("d1", "gen.power")]


# ---------------------------------------------------------------------------
# 6. Priority-based matching
# ---------------------------------------------------------------------------


class TestPriority:
    def test_higher_priority_wins(self) -> None:
        rules = [
            _make_rule("low-prio", ["file"], priority=0),
            _make_rule("high-prio", ["kafka"], priority=100),
        ]
        points = [PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr"))]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        result = router.route([_make_pv("d1", "p1")])
        assert result == {"kafka": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 7. Same priority — declaration order wins
# ---------------------------------------------------------------------------


class TestSamePriority:
    def test_declaration_order_wins_on_same_priority(self) -> None:
        # Both rules match everything, same priority — first declared wins
        rules = [
            _make_rule("first", ["kafka"], priority=0),
            _make_rule("second", ["file"], priority=0),
        ]
        points = [PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr"))]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        result = router.route([_make_pv("d1", "p1")])
        assert result == {"kafka": [_make_pv("d1", "p1")]}


# ---------------------------------------------------------------------------
# 8. Unmatched point — dropped silently
# ---------------------------------------------------------------------------


class TestUnmatched:
    def test_unmatched_point_dropped(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [
            PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr")),
            PointConfig(point_id="p2", device_id="d2", address=PointAddress(type="hr")),
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
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
            _make_rule("d1-rule", ["kafka"], match_device="d1"),
            _make_rule("d2-rule", ["file"], match_device="d2"),
            _make_rule("rotor-rule", ["archive"], match_point_prefix="rotor."),
        ]
        points = [
            PointConfig(point_id="rotor.speed", device_id="d1", address=PointAddress(type="hr")),
            PointConfig(point_id="gen.power", device_id="d1", address=PointAddress(type="hr")),
            PointConfig(point_id="rotor.speed", device_id="d2", address=PointAddress(type="hr")),
        ]
        # d1/rotor.speed → d1-rule matches first (priority 0, device match)
        # d1/gen.power → d1-rule matches first
        # d2/rotor.speed → d2-rule matches first
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        batch = [
            _make_pv("d1", "rotor.speed"),
            _make_pv("d1", "gen.power"),
            _make_pv("d2", "rotor.speed"),
        ]
        result = router.route(batch)
        assert set(result.keys()) == {"kafka", "file"}
        assert len(result["kafka"]) == 2  # d1/rotor.speed + d1/gen.power
        assert len(result["file"]) == 1  # d2/rotor.speed


# ---------------------------------------------------------------------------
# 10. explain — returns correct RouteDecision
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_point_override(self) -> None:
        rules = [_make_rule("default", ["kafka"])]
        points = [
            PointConfig(
                point_id="p1",
                device_id="d1",
                address=PointAddress(type="hr"),
                sinks=["override_sink"],
            )
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        decision = router.explain("d1", "p1")
        assert decision.targets == ["override_sink"]
        assert decision.matched_rule is None
        assert decision.source == "point_override"

    def test_explain_rule_match(self) -> None:
        rules = [_make_rule("my-rule", ["kafka"], match_device="d1")]
        points = [PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr"))]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        decision = router.explain("d1", "p1")
        assert decision.targets == ["kafka"]
        assert decision.matched_rule == "my-rule"
        assert decision.source == "rule"

    def test_explain_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [PointConfig(point_id="p2", device_id="d2", address=PointAddress(type="hr"))]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        decision = router.explain("d2", "p2")
        assert decision.targets == []
        assert decision.matched_rule is None
        assert decision.source == "unmatched"


# ---------------------------------------------------------------------------
# 11. unmatched_points — returns correct list
# ---------------------------------------------------------------------------


class TestUnmatchedPoints:
    def test_unmatched_points_list(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [
            PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr")),
            PointConfig(point_id="p2", device_id="d2", address=PointAddress(type="hr")),
        ]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        unmatched = router.unmatched_points()
        assert unmatched == [("d2", "p2")]

    def test_all_matched_no_unmatched(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"])]
        points = [PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr"))]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        assert router.unmatched_points() == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_batch(self) -> None:
        rules = [_make_rule("default", ["kafka"])]
        points = [PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr"))]
        table = RoutingTable(rules=rules, points=points)
        router = Router(table)
        result = router.route([])
        assert result == {}

    def test_table_size(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"])]
        points = [
            PointConfig(point_id="p1", device_id="d1", address=PointAddress(type="hr")),
            PointConfig(point_id="p2", device_id="d1", address=PointAddress(type="hr")),
        ]
        table = RoutingTable(rules=rules, points=points)
        assert table.size == 2
        router = Router(table)
        assert router.table_size == 2
