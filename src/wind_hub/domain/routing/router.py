"""Router — pure domain component for per-point data routing."""

from __future__ import annotations

from wind_hub.config.routing import RoutingTable
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.model.route import RouteDecision


class Router:
    """Per-point router — maps PointValues to sink-name buckets.

    Pure logic, no IO, no async.  The routing table is read-only
    after construction.
    """

    def __init__(self, table: RoutingTable) -> None:
        self._table = table

    def route(self, batch: list[PointValue]) -> dict[str, list[PointValue]]:
        """Route a batch of point values to their target sinks.

        Args:
            batch: Collected point values from one or more devices.

        Returns:
            ``{sink_name: [PointValue, …], …}``.  Points that match
            no rule are silently dropped from the output.  A single
            point may appear in multiple sink lists (fan-out).
            Within each sink's list, values keep the input ordering.
        """
        result: dict[str, list[PointValue]] = {}
        for pv in batch:
            targets = self._table.resolve(pv.device_id, pv.point_id)
            for sn in targets:
                if sn not in result:
                    result[sn] = []
                result[sn].append(pv)
        return result

    def explain(self, device_id: str, point_id: str) -> RouteDecision:
        """Explain the routing decision for a single point.

        Useful for CLI debugging commands like
        ``wind-hub route explain wtg-001 rotor.speed``.
        """
        targets, matched_rule, source = self._table.explain(device_id, point_id)
        return RouteDecision(
            device_id=device_id,
            point_id=point_id,
            targets=targets,
            matched_rule=matched_rule,
            source=source,
        )

    def unmatched_points(self) -> list[tuple[str, str]]:
        """Return all points that have no route target."""
        return self._table.unmatched_points()

    def rule_for(self, device_id: str, point_id: str) -> str | None:
        """返回命中该点的规则名（点位级覆盖/未匹配为 ``None``）。"""
        return self._table.rule_for(device_id, point_id)

    @property
    def table_size(self) -> int:
        """Number of entries in the underlying routing table."""
        return self._table.size
