"""Reliable Delivery source selection, settlement and retry policies."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.delivery_point import DeliveryPoint, select_delivery_points
from sink_service.application.realtime import BatchOutcome


@dataclass(frozen=True, slots=True)
class ReliableSelection:
    items: tuple[SampleItem, ...]
    points: tuple[DeliveryPoint, ...]
    key: bytes
    stripe: int
    filter_reason: str = ""

    @property
    def count(self) -> int:
        return len(self.items) if self.items else len(self.points)


@dataclass(frozen=True, slots=True)
class SettlementDecision:
    delivered: int
    permanent: tuple[int, ...]
    unresolved: bool


def select_reliable(
    batch: SampleBatch, source: Mapping[str, Any], *, kafka: bool
) -> ReliableSelection:
    """Apply the authoritative source selector, never untrusted NATS headers."""
    if batch.wind_farm_id != source["wind_farm_id"]:
        return ReliableSelection((), (), b"", 0, "wind_farm_mismatch")
    if batch.collector_id != source["collector_id"]:
        return ReliableSelection((), (), b"", 0, "collector_mismatch")
    if source["devices"] and batch.device_id not in source["devices"]:
        return ReliableSelection((), (), b"", 0, "device_mismatch")
    point_ids = frozenset(source["points"]) if source["points"] else None
    if kafka:
        items = tuple(
            item for item in batch.samples if point_ids is None or item.point_id in point_ids
        )
        key = ":".join((batch.wind_farm_id, batch.collector_id, batch.device_id)).encode("utf-8")
        stripe = hash((batch.wind_farm_id, batch.collector_id, batch.device_id)) % 128
        return ReliableSelection(items, (), key, stripe, "" if items else "point_filter_no_match")
    points = select_delivery_points(batch, point_ids)
    return ReliableSelection((), points, b"", 0, "" if points else "point_filter_no_match")


def classify_settlement(outcome: BatchOutcome, length: int, *, kafka: bool) -> SettlementDecision:
    """A terminal result requires confirmed delivery or explicit permanent evidence."""
    settled = set(outcome.settled)
    delivered = set(outcome.delivered)
    if (
        not delivered.issubset(settled)
        or len(settled) != len(outcome.settled)
        or any(not 0 <= index < length for index in settled)
    ):
        raise RuntimeError("Adapter returned invalid batch indexes")
    permanent = set(outcome.permanent)
    if len(permanent) != len(outcome.permanent) or not permanent.issubset(settled - delivered):
        raise RuntimeError("Adapter returned invalid permanent indexes")
    if permanent and kafka:
        raise RuntimeError("Kafka output cannot classify permanent point failures")
    return SettlementDecision(
        delivered=len(delivered),
        permanent=tuple(sorted(permanent)),
        unresolved=len(delivered | permanent) != length or bool(outcome.error and not permanent),
    )
