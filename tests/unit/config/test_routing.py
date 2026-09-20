"""Unit tests for the pre-compiled RoutingTable."""

from __future__ import annotations

import pytest

from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.point import PointRef
from wind_hub.domain.model.route import RouteMatch, RouteRule, RouteTarget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_point(
    point_id: str,
    sinks: list[str] | None = None,
    group: str = "default",
) -> PointConfig:
    return PointConfig(
        point_id=point_id,
        group=group,
        address=PointAddress(type="hr", register=30001),
        sinks=sinks,
    )


def _pbd(*entries: tuple[str, list[PointConfig]]) -> dict[str, list[PointConfig]]:
    """Build a ``{device_id: [PointConfig, …]}`` mapping from tuples."""
    return dict(entries)


# ---------------------------------------------------------------------------
# 1. resolve — correct results
# ---------------------------------------------------------------------------


class TestResolve:
    def test_resolve_returns_targets(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        table = RoutingTable(rules=rules, points_by_device=_pbd(("d1", [_make_point("p1")])))
        assert table.resolve("d1", "p1") == ["kafka"]

    def test_resolve_returns_empty_for_unknown_point(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        table = RoutingTable(rules=rules, points_by_device=_pbd(("d1", [_make_point("p1")])))
        assert table.resolve("d1", "unknown") == []

    def test_resolve_point_override(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        table = RoutingTable(
            rules=rules, points_by_device=_pbd(("d1", [_make_point("p1", sinks=["override"])]))
        )
        assert table.resolve("d1", "p1") == ["override"]


# ---------------------------------------------------------------------------
# 2. resolve_batch — bulk lookup
# ---------------------------------------------------------------------------


class TestResolveBatch:
    def test_resolve_batch_returns_all_matches(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"], all=True)]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d1", [_make_point("p1"), _make_point("p2")])),
        )
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
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d1", [_make_point("p1")]), ("d2", [_make_point("p2")])),
            unmatched_policy="drop",
            device_groups={"d1": "turbine", "d2": "pcs"},
        )
        assert table.resolve("d2", "p2") == []
        assert ("d2", "p2") in table.unmatched_points()


# ---------------------------------------------------------------------------
# 4. unmatched_policy="error" — raises ConfigError
# ---------------------------------------------------------------------------


class TestPolicyError:
    def test_unmatched_policy_error_raises(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        with pytest.raises(ConfigError, match="Unmatched"):
            RoutingTable(
                rules=rules,
                points_by_device=_pbd(("d1", [_make_point("p1")]), ("d2", [_make_point("p2")])),
                unmatched_policy="error",
                device_groups={"d1": "turbine", "d2": "pcs"},
            )

    def test_unmatched_policy_error_passes_when_all_matched(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"], all=True)]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d1", [_make_point("p1")])),
            unmatched_policy="error",
        )
        assert table.size == 1


# ---------------------------------------------------------------------------
# 5. unmatched_points — lists all unmatched
# ---------------------------------------------------------------------------


class TestUnmatchedPoints:
    def test_unmatched_points_all_listed(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(
                ("d1", [_make_point("p1")]),
                ("d2", [_make_point("p2")]),
                ("d3", [_make_point("p3")]),
            ),
            device_groups={"d1": "turbine", "d2": "pcs", "d3": "pcs"},
        )
        assert sorted(table.unmatched_points()) == [("d2", "p2"), ("d3", "p3")]


# ---------------------------------------------------------------------------
# 6. size — correct count
# ---------------------------------------------------------------------------


class TestSize:
    def test_size_matches_matched_points(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"], all=True)]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(
                ("d1", [_make_point("p1"), _make_point("p2"), _make_point("p3")])
            ),
        )
        assert table.size == 3

    def test_size_excludes_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d1", [_make_point("p1")]), ("d2", [_make_point("p2")])),
            device_groups={"d1": "turbine", "d2": "pcs"},
        )
        assert table.size == 1  # only d1/p1 matched


# ---------------------------------------------------------------------------
# match 维度 — device_group / point_group / AND / all
# ---------------------------------------------------------------------------


class TestMatchDimensions:
    def test_device_group_and_point_group_and(self) -> None:
        """device_group 与 point_group 同时配置时为 AND：两个维度都满足才命中。"""
        rules = [
            _make_rule(
                "and-rule", ["kafka"], device_group="turbine", point_group="fast"
            )
        ]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(
                ("d1", [_make_point("p-fast", group="fast"), _make_point("p-slow", group="slow")]),
                ("d2", [_make_point("p-fast", group="fast")]),
            ),
            device_groups={"d1": "turbine", "d2": "pcs"},
        )
        # turbine + fast → 命中
        assert table.resolve("d1", "p-fast") == ["kafka"]
        # turbine 但 slow 组 → 不命中
        assert table.resolve("d1", "p-slow") == []
        # fast 组但非 turbine → 不命中
        assert table.resolve("d2", "p-fast") == []

    def test_device_group_only(self) -> None:
        rules = [_make_rule("turbine-only", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(
                ("d1", [_make_point("p1", group="fast"), _make_point("p2", group="slow")]),
                ("d2", [_make_point("p3")]),
            ),
            device_groups={"d1": "turbine", "d2": None},
        )
        assert table.resolve("d1", "p1") == ["kafka"]
        assert table.resolve("d1", "p2") == ["kafka"]
        # 无 device_group 的设备不匹配 device_group 条件
        assert table.resolve("d2", "p3") == []

    def test_point_group_only(self) -> None:
        rules = [_make_rule("fast-only", ["kafka"], point_group="fast")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(
                ("d1", [_make_point("p1", group="fast")]),
                ("d2", [_make_point("p2", group="fast"), _make_point("p3", group="slow")]),
            ),
            device_groups={"d1": "turbine", "d2": "pcs"},
        )
        assert table.resolve("d1", "p1") == ["kafka"]
        assert table.resolve("d2", "p2") == ["kafka"]
        assert table.resolve("d2", "p3") == []

    def test_all_true_matches_everything(self) -> None:
        rules = [_make_rule("catch-all", ["kafka"], all=True)]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(
                ("d1", [_make_point("p1", group="fast")]),
                ("d2", [_make_point("p2", group="slow")]),
            ),
            device_groups={"d1": "turbine", "d2": None},
        )
        assert table.resolve("d1", "p1") == ["kafka"]
        assert table.resolve("d2", "p2") == ["kafka"]

    def test_precompiled_first_match_wins_by_priority(self) -> None:
        """预编译表按 priority 降序 first-match：高优先级规则决定目标。"""
        rules = [
            _make_rule("low", ["file"], priority=0, all=True),
            _make_rule(
                "high", ["kafka"], priority=100, device_group="turbine", point_group="fast"
            ),
        ]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(
                ("d1", [_make_point("p-fast", group="fast"), _make_point("p-slow", group="slow")])
            ),
            device_groups={"d1": "turbine"},
        )
        assert table.resolve("d1", "p-fast") == ["kafka"]
        assert table.rule_for("d1", "p-fast") == "high"
        assert table.resolve("d1", "p-slow") == ["file"]
        assert table.rule_for("d1", "p-slow") == "low"


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_overridden(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        table = RoutingTable(
            rules=rules, points_by_device=_pbd(("d1", [_make_point("p1", sinks=["override"])]))
        )
        targets, rule_name, source = table.explain("d1", "p1")
        assert targets == ["override"]
        assert rule_name is None
        assert source == "point_override"

    def test_explain_rule(self) -> None:
        rules = [_make_rule("my-rule", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d1", [_make_point("p1")])),
            device_groups={"d1": "turbine"},
        )
        targets, rule_name, source = table.explain("d1", "p1")
        assert targets == ["kafka"]
        assert rule_name == "my-rule"
        assert source == "rule"

    def test_explain_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d2", [_make_point("p2")])),
            device_groups={"d2": "pcs"},
        )
        targets, rule_name, source = table.explain("d2", "p2")
        assert targets == []
        assert rule_name is None
        assert source == "unmatched"


# ---------------------------------------------------------------------------
# rule_for — 命中规则名查询（Delivery Policy 用）
# ---------------------------------------------------------------------------


class TestRuleFor:
    def test_rule_for_returns_matching_rule(self) -> None:
        rules = [_make_rule("my-rule", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d1", [_make_point("p1")])),
            device_groups={"d1": "turbine"},
        )
        assert table.rule_for("d1", "p1") == "my-rule"

    def test_rule_for_none_for_point_override(self) -> None:
        rules = [_make_rule("default", ["kafka"], all=True)]
        table = RoutingTable(
            rules=rules, points_by_device=_pbd(("d1", [_make_point("p1", sinks=["override"])]))
        )
        assert table.rule_for("d1", "p1") is None

    def test_rule_for_none_for_unmatched(self) -> None:
        rules = [_make_rule("specific", ["kafka"], device_group="turbine")]
        table = RoutingTable(
            rules=rules,
            points_by_device=_pbd(("d2", [_make_point("p2")])),
            device_groups={"d2": "pcs"},
        )
        assert table.rule_for("d2", "p2") is None


# ---------------------------------------------------------------------------
# invalid policy
# ---------------------------------------------------------------------------


class TestInvalidPolicy:
    def test_invalid_unmatched_policy_raises(self) -> None:
        with pytest.raises(ConfigError, match="unmatched_policy"):
            RoutingTable(rules=[], points_by_device={}, unmatched_policy="invalid")
