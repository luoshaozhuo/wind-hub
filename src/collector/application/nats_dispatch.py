"""Collector-side sample serialization and dispatch, independent of output Sinks."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Protocol

from contracts.sample_batch import MAX_SAMPLES_PER_BATCH, BatchSample, SampleBatch
from contracts.sample_message import DeliveryMode
from core.domain.point_value import PointValue

from .identity import CollectorIdentity

logger = logging.getLogger(__name__)


class PublisherPort(Protocol):
    """Minimal publishing dependency supplied by infrastructure."""

    @property
    def connected(self) -> bool: ...

    async def open(self) -> None:
        """Initialize the NATS connection."""
        ...

    async def close(self) -> None:
        """Release NATS resources."""
        ...

    async def publish(self, message: SampleBatch) -> bool:
        """Return whether publication was accepted."""
        ...


class SamplePublisher:
    """Convert one device acquisition cycle into bounded batch messages."""

    def __init__(
        self,
        publisher: PublisherPort,
        *,
        wind_farm_id: str,
        identity: CollectorIdentity,
        delivery: DeliveryMode,
        max_staleness_ms: int | None,
        queue_capacity: int = 2048,
    ) -> None:
        if delivery == "realtime" and (max_staleness_ms is None or max_staleness_ms <= 0):
            raise ValueError("realtime delivery requires a positive max_staleness_ms")
        self._publisher = publisher
        self._wind_farm_id = wind_farm_id
        self._identity = identity
        self._delivery = delivery
        self._max_staleness_ms = max_staleness_ms if delivery == "realtime" else None
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        self._queue: asyncio.Queue[SampleBatch] = asyncio.Queue(maxsize=queue_capacity)
        self._worker: asyncio.Task[None] | None = None
        self._sequence = 0
        self._points_published = 0
        self._points_dropped = 0

    @property
    def connected(self) -> bool:
        """Return current NATS transport readiness."""
        return self._publisher.connected

    async def open(self) -> None:
        """Open the NATS publisher."""
        await self._publisher.open()
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="sample-nats-dispatch")

    async def close(self) -> None:
        """Close the NATS publisher."""
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=3.0)
            except TimeoutError:
                logger.warning("NATS sample queue not fully drained at shutdown")
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await self._publisher.close()

    async def _run(self) -> None:
        """Independent bounded publish worker; sender errors never stop the loop."""
        while True:
            message = await self._queue.get()
            try:
                try:
                    delivered = await self._publisher.publish(message)
                except Exception:
                    delivered = False
                    logger.exception("NATS sample dispatch failed")
                if delivered:
                    self._points_published += len(message.samples)
                else:
                    self._points_dropped += len(message.samples)
            finally:
                self._queue.task_done()

    async def dispatch(self, task_id: str, values: list[PointValue]) -> None:
        """Queue device-scoped batches; never split or mix source identities."""
        grouped: dict[str, list[PointValue]] = {}
        for value in values:
            grouped.setdefault(value.device_id, []).append(value)
        for device_id, group in grouped.items():
            for offset in range(0, len(group), MAX_SAMPLES_PER_BATCH):
                chunk = group[offset : offset + MAX_SAMPLES_PER_BATCH]
                entries: list[BatchSample] = []
                for value in chunk:
                    self._sequence += 1
                    try:
                        entries.append(
                            BatchSample(
                                point_id=value.point_id,
                                sample_id=(
                                    f"{self._identity.collector_id}:"
                                    f"{self._identity.boot_id}:{self._sequence}"
                                ),
                                value=value.value,
                                quality=value.quality.value,
                                sample_time=value.timestamp,
                                receive_time=value.receive_time,
                                max_staleness_ms=self._max_staleness_ms,
                            )
                        )
                    except (ValueError, TypeError):
                        logger.exception("Invalid sample task=%s point=%s", task_id, value.point_id)
                        self._points_dropped += 1
                if not entries:
                    continue
                try:
                    batch = SampleBatch(
                        batch_id=entries[0].sample_id,
                        mode=self._delivery,
                        wind_farm_id=self._wind_farm_id,
                        collector_id=self._identity.collector_id,
                        task_id=task_id,
                        device_id=device_id,
                        publish_time=datetime.now(UTC),
                        samples=tuple(entries),
                    )
                    self._queue.put_nowait(batch)
                except (ValueError, TypeError, asyncio.QueueFull):
                    logger.warning(
                        "Batch rejected task=%s device=%s count=%d",
                        task_id,
                        device_id,
                        len(entries),
                    )
                    self._points_dropped += len(entries)

    @property
    def points_published(self) -> int:
        """Number of successfully published samples."""
        return self._points_published

    @property
    def points_dropped(self) -> int:
        """Number of samples not confirmed as published."""
        return self._points_dropped
