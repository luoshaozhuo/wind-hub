"""Route table — compiles RouteRules + PointConfigs into an O(1) lookup structure.

The table is built once at config-load time.  Runtime lookups are
dictionary lookups — no rule-walking per point.
"""

from __future__ import annotations

from wind_hub.config.schema import PointConfig
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.point import PointRef
from wind_hub.domain.model.route import RouteRule

ALLOWED_UNMATCHED_POLICIES = frozenset({"drop", "error", "default"})


class RoutingTable:
    """Pre-compiled routing table for O(1) point-to-sinks resolution.

    Built from the full ``RouteRule`` list plus per-device point sets
    (``{device_id: [PointConfig, …]}``——点表已与设备绑定解析）so
    that every ``resolve()`` call is a dictionary lookup.
    """

    def __init__(
        self,
        rules: list[RouteRule],
        points_by_device: dict[str, list[PointConfig]],
        unmatched_policy: str = "drop",
    ) -> None:
        if unmatched_policy not in ALLOWED_UNMATCHED_POLICIES:
            raise ConfigError(
                f"Invalid unmatched_policy '{unmatched_policy}'; "
                f"must be one of {sorted(ALLOWED_UNMATCHED_POLICIES)}"
            )
        self._unmatched_policy = unmatched_policy
        self._rules = sorted(rules, key=lambda r: (-r.priority, r.name))

        # Per-point sink overrides: (device_id, point_id) → [sink, …]
        self._point_overrides: dict[tuple[str, str], list[str]] = {}
        for device_id, points in points_by_device.items():
            for p in points:
                if p.sinks is not None:
                    self._point_overrides[(device_id, p.point_id)] = list(p.sinks)

        # Build the full pre-compiled table
        self._table: dict[tuple[str, str], list[str]] = {}
        self._rule_hits: dict[tuple[str, str], str] = {}
        """Records which rule name matched, for RouteDecision."""

        unmatched: list[tuple[str, str]] = []

        for device_id, points in points_by_device.items():
            for p in points:
                key = (device_id, p.point_id)

                # 1. Explicit per-point sinks override everything
                if key in self._point_overrides:
                    self._table[key] = list(self._point_overrides[key])
                    # matched_rule stays None; Router sets source='point_override'
                    continue

                # 2. Walk rules by descending priority
                targets = self._match_rules(device_id, p.point_id)
                if targets:
                    self._table[key] = targets
                    # Record first matching rule name
                    self._rule_hits[key] = self._first_matching_rule_name(
                        device_id, p.point_id
                    )
                else:
                    unmatched.append(key)

        if self._unmatched_policy == "error" and unmatched:
            raise ConfigError(f"Unmatched points found (unmatched_policy='error'): " f"{unmatched}")

        self._unmatched = unmatched

    # ------------------------------------------------------------------
    # public query API
    # ------------------------------------------------------------------

    def resolve(self, device_id: str, point_id: str) -> list[str]:
        """O(1) lookup — return the sink name list for a point.

        Returns an empty list when the point has no match and
        ``unmatched_policy`` is ``'drop'``.
        """
        return list(self._table.get((device_id, point_id), []))

    def resolve_batch(self, points: list[PointRef]) -> dict[tuple[str, str], list[str]]:
        """Batch O(n) lookup for multiple points."""
        result: dict[tuple[str, str], list[str]] = {}
        for ref in points:
            key = (ref.device_id, ref.point_id)
            targets = self._table.get(key)
            if targets:
                result[key] = list(targets)
        return result

    def unmatched_points(self) -> list[tuple[str, str]]:
        """Return all (device_id, point_id) keys with no route target."""
        return list(self._unmatched)

    def rule_for(self, device_id: str, point_id: str) -> str | None:
        """返回命中该点的规则名；点位级覆盖或未匹配时返回 ``None``。

        Delivery Policy 按规则评估投递节拍时以此定位点所属规则。
        """
        return self._rule_hits.get((device_id, point_id))

    def explain(self, device_id: str, point_id: str) -> tuple[list[str], str | None, str]:
        """Return (targets, matched_rule, source) for a single point.

        source is one of ``'point_override'``, ``'rule'``, ``'unmatched'``.
        """
        key = (device_id, point_id)
        if key in self._point_overrides:
            return (list(self._point_overrides[key]), None, "point_override")
        if key in self._table:
            return (list(self._table[key]), self._rule_hits.get(key), "rule")
        return ([], None, "unmatched")

    @property
    def size(self) -> int:
        """Number of entries in the pre-compiled table."""
        return len(self._table)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _match_rules(self, device_id: str, point_id: str) -> list[str] | None:
        """Walk sorted rules; return sink names of the first matching rule."""
        for rule in self._rules:
            if self._rule_matches(rule, device_id, point_id):
                return [t.sink for t in rule.targets]
        return None

    def _first_matching_rule_name(self, device_id: str, point_id: str) -> str:
        """Return the name of the first matching rule (caller guarantees a match)."""
        for rule in self._rules:
            if self._rule_matches(rule, device_id, point_id):
                return rule.name
        return ""  # unreachable when a match already exists

    @staticmethod
    def _rule_matches(rule: RouteRule, device_id: str, point_id: str) -> bool:
        device_ok = rule.match_device is None or rule.match_device == device_id
        prefix_ok = rule.match_point_prefix is None or point_id.startswith(rule.match_point_prefix)
        return device_ok and prefix_ok
