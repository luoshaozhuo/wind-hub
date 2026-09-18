"""Unit tests for the pre-compiled RoutingTable."""

from __future__ import annotations

import pytest

from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.point import PointRef
from wind_hub.domain.model.route import RouteRule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_point(
    point_id: str,
    device_id: str = "d1",
    sinks: list[str] | None = None,
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        device_id=device_id,
        address=PointAddress(type="hr", register=30001),
        sinks=sinks,
    )


# ---------------------------------------------------------------------------
# 1. resolve — correct results
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_returns_targets(self) -> None:
        rules = [_make_rule("default", ["kafka"])]
        points = [_make_point("p1")]
        table = RoutingTable(rules=rules, points=points)
        assert table.resolve("d1", "p1") == ["kafka"]

    def test_resolve_returns_empty_for_unknown_point(self) -> None:
        rules = [_make_rule("default", ["kafka"])]
        points = [_make_point("p1")]
        table = RoutingTable(rules=rules, points=points)
        assert table.resolve("d1", "unknown") == []

    def test_resolve_point_override(self) -> None:
        rules = [_make_rule("default", ["kafka"])]
        points = [_make_point("p1", sinks=["override"])]
        table = RoutingTable(rules=rules, points=points)
        assert table.resolve("d1", "p1") == ["override"]


# ---------------------------------------------------------------------------
# 2. resolve_batch — bulk lookup
# ---------------------------------------------------------------------------


class TestResolveBatch:
    def test_resolve_batch_returns_all_matches(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"])]
        points = [
            _make_point("p1"),
            _make_point("p2"),
        ]
        table = RoutingTable(rules=rules, points=points)
        refs = [
            PointRef(device_id="d1", point_id="p1"),
            PointRef(device_id="d1", point_id="p2"),
            PointRef(device_id="d1", point_id="unknown"),
        ]
        result = table.resolve_batch(refs)
        assert result == {
            ("d1", "p1"): ["kafka"],
            ("d1", "p2"): ["kafka"],
        }


# ---------------------------------------------------------------------------
# 3. unmatched_policy="drop" — unmatched returns empty
# ---------------------------------------------------------------------------


class TestPolicyDrop:
    def test_unmatched_returns_empty_with_drop(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [
            _make_point("p1", device_id="d1"),
            _make_point("p2", device_id="d2"),  # no matching rule
        ]
        table = RoutingTable(rules=rules, points=points, unmatched_policy="drop")
        assert table.resolve("d2", "p2") == []
        assert ("d2", "p2") in table.unmatched_points()


# ---------------------------------------------------------------------------
# 4. unmatched_policy="error" — raises ConfigError
# ---------------------------------------------------------------------------


class TestPolicyError:
    def test_unmatched_policy_error_raises(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [
            _make_point("p1", device_id="d1"),
            _make_point("p2", device_id="d2"),
        ]
        with pytest.raises(ConfigError, match="Unmatched"):
            RoutingTable(rules=rules, points=points, unmatched_policy="error")

    def test_unmatched_policy_error_passes_when_all_matched(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"])]
        points = [_make_point("p1")]
        table = RoutingTable(rules=rules, points=points, unmatched_policy="error")
        assert table.size == 1


# ---------------------------------------------------------------------------
# 5. unmatched_points — lists all unmatched
# ---------------------------------------------------------------------------


class TestUnmatchedPoints:
    def test_unmatched_points_all_listed(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [
            _make_point("p1", device_id="d1"),
            _make_point("p2", device_id="d2"),
            _make_point("p3", device_id="d3"),
        ]
        table = RoutingTable(rules=rules, points=points)
        assert sorted(table.unmatched_points()) == [("d2", "p2"), ("d3", "p3")]


# ---------------------------------------------------------------------------
# 6. size — correct count
# ---------------------------------------------------------------------------


class TestSize:
    def test_size_matches_matched_points(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"])]
        points = [
            _make_point("p1"),
            _make_point("p2"),
            _make_point("p3"),
        ]
        table = RoutingTable(rules=rules, points=points)
        assert table.size == 3

    def test_size_excludes_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [
            _make_point("p1", device_id="d1"),
            _make_point("p2", device_id="d2"),
        ]
        table = RoutingTable(rules=rules, points=points)
        assert table.size == 1  # only d1/p1 matched


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_overridden(self) -> None:
        rules = [_make_rule("default", ["kafka"])]
        points = [_make_point("p1", sinks=["override"])]
        table = RoutingTable(rules=rules, points=points)
        targets, rule_name, source = table.explain("d1", "p1")
        assert targets == ["override"]
        assert rule_name is None
        assert source == "point_override"

    def test_explain_rule(self) -> None:
        rules = [_make_rule("my-rule", ["kafka"], match_device="d1")]
        points = [_make_point("p1")]
        table = RoutingTable(rules=rules, points=points)
        targets, rule_name, source = table.explain("d1", "p1")
        assert targets == ["kafka"]
        assert rule_name == "my-rule"
        assert source == "rule"

    def test_explain_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], match_device="d1")]
        points = [_make_point("p2", device_id="d2")]
        table = RoutingTable(rules=rules, points=points)
        targets, rule_name, source = table.explain("d2", "p2")
        assert targets == []
        assert rule_name is None
        assert source == "unmatched"


# ---------------------------------------------------------------------------
# invalid policy
# ---------------------------------------------------------------------------


class TestInvalidPolicy:
    def test_invalid_unmatched_policy_raises(self) -> None:
        with pytest.raises(ConfigError, match="unmatched_policy"):
            RoutingTable(rules=[], points=[], unmatched_policy="invalid")
