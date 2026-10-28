"""Collector-side sample serialization and dispatch, independent of output Sinks."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from contracts.sample_message import DeliveryMode, SampleMessage
from core.domain.point_value import PointValue

from .identity import CollectorIdentity

logger = logging.getLogger(__name__)


class PublisherPort(Protocol):
    """Minimal publishing dependency supplied by infrastructure."""

    async def open(self) -> None:
        """Initialize the NATS connection."""
        ...

    async def close(self) -> None:
        """Release NATS resources."""
        ...

    async def publish(self, message: SampleMessage) -> bool:
        """Return whether publication was accepted."""
        ...


class SamplePublisher:
    """Convert acquired point values into single-sample messages."""

    def __init__(
        self,
        publisher: PublisherPort,
        *,
        wind_farm_id: str,
        identity: CollectorIdentity,
        delivery: DeliveryMode,
        max_staleness_ms: int | None,
    ) -> None:
        if delivery == "realtime" and (max_staleness_ms is None or max_staleness_ms <= 0):
            raise ValueError("realtime delivery requires a positive max_staleness_ms")
        self._publisher = publisher
        self._wind_farm_id = wind_farm_id
        self._identity = identity
        self._delivery = delivery
        self._max_staleness_ms = max_staleness_ms if delivery == "realtime" else None
        self._sequence = 0
        self._points_published = 0
        self._points_dropped = 0

    async def open(self) -> None:
        """Open the NATS publisher."""
        await self._publisher.open()

    async def close(self) -> None:
        """Close the NATS publisher."""
        await self._publisher.close()

    async def dispatch(self, task_id: str, values: list[PointValue]) -> None:
        """Publish each sample independently; failures never abort acquisition."""
        failed = False
        for value in values:
            self._sequence += 1
            if failed:
                self._points_dropped += 1
                continue
            try:
                message = SampleMessage(
                    wind_farm_id=self._wind_farm_id,
                    collector_id=self._identity.collector_id,
                    task_id=task_id,
                    device_id=value.device_id,
                    point_id=value.point_id,
                    sample_id=f"{self._identity.collector_id}:{self._identity.boot_id}:{self._sequence}",
                    value=value.value,
                    quality=cast(Literal["good", "bad", "uncertain"], value.quality.value),
                    sample_time=value.timestamp,
                    receive_time=value.receive_time,
                    publish_time=datetime.now(UTC),
                    max_staleness_ms=self._max_staleness_ms,
                )
                success = await self._publisher.publish(message)
            except Exception:
                logger.exception("Invalid/unpublished sample task=%s point=%s", task_id, value.point_id)
                success = False
            if success:
                self._points_published += 1
            else:
                self._points_dropped += 1
                failed = True

    @property
    def points_published(self) -> int:
        """Number of successfully published samples."""
        return self._points_published

    @property
    def points_dropped(self) -> int:
        """Number of samples not confirmed as published."""
        return self._points_dropped
