"""Sink Service delivery write port."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from sink_service.application.delivery_point import DeliveryPoint

if TYPE_CHECKING:
    from sink_service.application.realtime import BatchOutcome


class BatchWriter(Protocol):
    async def write_batch(self, samples: tuple[DeliveryPoint, ...]) -> BatchOutcome: ...
