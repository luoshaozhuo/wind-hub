"""Bounded Collector publisher using one immutable batch per device and channel."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import nats
from nats.aio.client import Client

from collector.application.telemetry import CollectorTelemetry
from contracts.sample_batch import (
    DEFAULT_MAX_BATCH_BYTES,
    DEFAULT_MAX_SAMPLES_PER_BATCH,
    SampleBatch,
    SampleItem,
    validate_subject_token,
)
from core.application import ProtocolSample

logger = logging.getLogger(__name__)


class CollectorPublisher:
    """Publish bounded immutable batches to Core NATS or JetStream."""

    def __init__(
        self,
        *,
        servers: list[str],
        site_id: str,
        collector_id: str,
        delivery: str,
        max_staleness_ms: int = 500,
        capacity: int = 2048,
        max_samples_per_batch: int = DEFAULT_MAX_SAMPLES_PER_BATCH,
        max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
        enqueue_timeout_s: float = 2.0,
        telemetry: CollectorTelemetry | None = None,
    ) -> None:
        if delivery not in {"realtime", "reliable"}:
            raise ValueError("unsupported delivery mode")
        if not servers or capacity <= 0:
            raise ValueError("NATS servers and positive queue capacity required")
        if not 0 < max_samples_per_batch <= 100_000 or max_batch_bytes <= 0:
            raise ValueError("invalid batch limits")
        if not 0 < enqueue_timeout_s <= 300:
            raise ValueError("enqueue_timeout_s must be in (0, 300]")
        if delivery == "realtime" and max_staleness_ms <= 0:
            raise ValueError("realtime max_staleness_ms must be positive")
        self._servers = list(servers)
        self._site_id = validate_subject_token(site_id)
        self._collector_id = validate_subject_token(collector_id)
        self._delivery: Literal["realtime", "reliable"] = (
            "realtime" if delivery == "realtime" else "reliable"
        )
        self._telemetry = telemetry
        self._staleness = max_staleness_ms
        self._max_samples = max_samples_per_batch
        self._max_bytes = max_batch_bytes
        self._enqueue_timeout_s = enqueue_timeout_s
        self._queue: asyncio.Queue[SampleBatch] = asyncio.Queue(maxsize=capacity)
        self._connection: Client | None = None
        self._task: asyncio.Task[None] | None = None
        self.last_error = ""
        self.attempted = 0
        self.published = 0
        self.failed = 0
        self.backpressure = 0
        self.expired = 0
        self.unconfirmed = 0
        self.not_attempted = 0
        self.connection_failed = 0
        self.dropped = 0
        self.client_submitted = 0
        self.pubacked = 0

    @property
    def collector_id(self) -> str:
        """Return stable producer identity."""
        return self._collector_id

    def update_site(self, site_id: str) -> None:
        """Change the site identity for subsequent batches only."""
        self._site_id = validate_subject_token(site_id)

    async def start(self) -> None:
        """Launch exactly one ordered publisher loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def _item(self, sample: ProtocolSample) -> SampleItem:
        return SampleItem(
            point_id=sample.point_id,
            sample_id=uuid4().hex,
            value=sample.value,
            quality=sample.quality.value,
            sample_time=sample.timestamp,
            receive_time=sample.receive_time,
            max_staleness_ms=self._staleness if self._delivery == "realtime" else None,
        )

    def _batch(self, device_id: str, samples: tuple[SampleItem, ...]) -> SampleBatch:
        return SampleBatch(
            batch_id=uuid4().hex,
            mode=self._delivery,
            wind_farm_id=self._site_id,
            collector_id=self._collector_id,
            task_id=device_id,
            device_id=device_id,
            publish_time=datetime.now(UTC),
            samples=samples,
        )

    def _pack(self, device_id: str, items: tuple[SampleItem, ...]) -> list[SampleBatch]:
        """Split without changing identities after the batches have been queued."""
        batches: list[SampleBatch] = []
        start = 0
        while start < len(items):
            count = min(self._max_samples, len(items) - start)
            candidate = self._batch(device_id, items[start : start + count])
            while count:
                try:
                    candidate.encode(max_samples=self._max_samples, max_bytes=self._max_bytes)
                    break
                except ValueError:
                    if count == 1:
                        raise
                    count //= 2
                    candidate = self._batch(device_id, items[start : start + count])
            batches.append(candidate)
            start += count
        return batches

    def validate_encoding(self, device_id: str, sample: ProtocolSample) -> None:
        """Validate the actual batch transport schema without sending it."""
        self._pack(device_id, (self._item(sample),))

    async def publish(self, device_id: str, samples: tuple[ProtocolSample, ...]) -> None:
        """Queue each complete source batch; reject invalid or duplicate points."""
        if not samples:
            return
        try:
            items = tuple(self._item(sample) for sample in samples)
            batches = self._pack(device_id, items)
        except (ValueError, TypeError) as exc:
            self.last_error = str(exc)
            self.failed += 1
            self.not_attempted += 1
            if self._telemetry is not None:
                self._telemetry.increment("collector.publisher.failed")
            return
        for batch in batches:
            try:
                self._queue.put_nowait(batch)
            except asyncio.QueueFull as exc:
                self.last_error = "batch queue capacity exhausted"
                self.backpressure += 1
                if self._telemetry is not None:
                    self._telemetry.increment("collector.publisher.backpressure")
                    self._telemetry.event(
                        "WARNING", "nats", "BACKPRESSURE",
                        f"batch queue full for {device_id}",
                    )
                if self._delivery == "reliable":
                    try:
                        await asyncio.wait_for(
                            self._queue.put(batch), timeout=self._enqueue_timeout_s
                        )
                    except TimeoutError as timeout_error:
                        self.not_attempted += 1
                        self.failed += 1
                        raise RuntimeError(
                            "reliable batch enqueue timed out; publication not accepted"
                        ) from timeout_error
                else:
                    self.not_attempted += 1
                    self.dropped += 1
                    if self._telemetry is not None:
                        self._telemetry.increment("collector.publisher.dropped")


    async def _connect(self) -> Client:
        if self._connection is not None and self._connection.is_connected:
            return self._connection
        if self._connection is not None:
            await self._connection.close()
        self._connection = await nats.connect(
            servers=self._servers,
            connect_timeout=2,
            max_reconnect_attempts=3,
        )
        return self._connection

    async def _send(self, batch: SampleBatch) -> None:
        connection = await self._connect()
        payload = batch.encode(
            max_samples=self._max_samples, max_bytes=self._max_bytes
        )
        subject = batch.subject()
        self.attempted += 1
        if self._telemetry is not None:
            self._telemetry.increment("collector.publisher.attempted")
        if self._delivery == "realtime":
            await connection.publish(subject, payload)
            self.client_submitted += 1
            if self._telemetry is not None:
                self._telemetry.increment("collector.publisher.client_submitted")
        else:
            # Retries, if initiated by the broker or an operator, retain the
            # original batch_id. PubAck is the only publication confirmation.
            await connection.jetstream().publish(
                subject, payload, timeout=2,
                headers={"Nats-Msg-Id": batch.batch_id},
            )
            self.pubacked += 1
            if self._telemetry is not None:
                self._telemetry.increment("collector.publisher.pubacked")
        self.published += 1
        if self._telemetry is not None:
            received = min(
                (item.receive_time or item.sample_time for item in batch.samples),
            )
            self._telemetry.observe_milliseconds(
                "collector.publisher.latency_ms",
                max(0.0, (datetime.now(UTC) - received).total_seconds() * 1000),
            )

    async def _run(self) -> None:
        while True:
            batch = await self._queue.get()
            try:
                delay = 0.2
                while True:
                    if self._delivery == "realtime":
                        now = datetime.now(UTC)
                        if all(
                            (now - sample.sample_time).total_seconds() * 1000
                            > (sample.max_staleness_ms or 0)
                            for sample in batch.samples
                        ):
                            self.expired += 1
                            self.dropped += 1
                            self.not_attempted += 1
                            if self._telemetry is not None:
                                self._telemetry.increment("collector.publisher.expired")
                                self._telemetry.increment("collector.publisher.dropped")
                            break
                    attempts_before = self.attempted
                    try:
                        await self._send(batch)
                        self.last_error = ""
                        break
                    except asyncio.CancelledError:
                        if self.attempted > attempts_before:
                            self.unconfirmed += 1
                            if self._telemetry is not None:
                                self._telemetry.increment("collector.publisher.unconfirmed")
                        raise
                    except (TypeError, ValueError) as exc:
                        self.last_error = str(exc)
                        self.failed += 1
                        self.not_attempted += 1
                        if self._telemetry is not None:
                            self._telemetry.increment("collector.publisher.failed")
                        break
                    except Exception as exc:
                        self.last_error = str(exc)
                        if self.attempted > attempts_before:
                            self.unconfirmed += 1
                            if self._telemetry is not None:
                                self._telemetry.increment("collector.publisher.unconfirmed")
                        else:
                            self.connection_failed += 1
                            if self._telemetry is not None:
                                self._telemetry.increment("collector.publisher.connection_failed")
                        logger.warning("Batch publication not confirmed: %s", exc)
                        if self._delivery == "realtime":
                            self.dropped += 1
                            if self._telemetry is not None:
                                self._telemetry.increment("collector.publisher.dropped")
                            break
                        # Reliable: retain the SAME batch and source identities
                        # until PubAck or an explicit process shutdown.
                        await asyncio.sleep(delay)
                        delay = min(30.0, delay * 2)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        """Drain queued batches and close the transport with finite deadlines."""
        if self._task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=2)
            except TimeoutError:
                while True:
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._queue.task_done()
                    self.dropped += 1
                    self.not_attempted += 1
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._connection is not None:
            try:
                await asyncio.wait_for(self._connection.drain(), timeout=2)
            except Exception:
                logger.warning("Collector NATS drain was not confirmed")
            finally:
                try:
                    await asyncio.wait_for(self._connection.close(), timeout=2)
                except TimeoutError:
                    logger.error("Collector NATS close exceeded deadline")
                finally:
                    self._connection = None
