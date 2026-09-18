"""Domain model — routing rule definitions."""

from __future__ import annotations

from pydantic import BaseModel


class RouteTarget(BaseModel):
    """Destination for routed data — a named Sink.

    Kept minimal by design.  Future extensions (e.g. per-target filter
    conditions) can be added without breaking route rules.
    """

    sink_name: str
    """Name of the SinkPort implementation to forward data to."""


class RouteRule(BaseModel):
    """A default routing rule that matches points and sends them to sinks.

    The router evaluates rules in descending priority order; the first
    matching rule determines the target sink list.
    """

    name: str
    """Human-readable rule name for debugging and observability."""

    match_device: str | None = None
    """Glob-like device ID filter (e.g. ``'turbine-*'``).
    ``None`` means "match any device"."""

    match_point_prefix: str | None = None
    """Prefix filter on point ID (e.g. ``'rotor.'`` matches
    ``'rotor.speed'`` but not ``'gen.power'``).
    ``None`` means "match any point"."""

    targets: list[str]
    """Ordered list of sink names to deliver matching data to."""

    priority: int = 0
    """Rule priority — higher values are evaluated first.
    Used to resolve conflicts when multiple rules could match."""


class RouteDecision(BaseModel):
    """A single routing decision, used for debugging and observability.

    Records *why* a particular (device_id, point_id) was routed to
    certain sinks.
    """

    device_id: str
    """Device identifier."""

    point_id: str
    """Point identifier."""

    targets: list[str]
    """Sink names this point is routed to."""

    matched_rule: str | None = None
    """Name of the RouteRule that matched, or ``None`` when the
    decision came from a per-point sink override."""

    source: str
    """How the targets were determined:
    ``'point_override'``, ``'rule'``, or ``'unmatched'``."""
