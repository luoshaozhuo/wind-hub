"""Bounded Collector publisher using one immutable batch per device and channel."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Literal

import nats
from nats.aio.client import Client

from collector.application.batch_builder import BatchBuilder
from collector.application.telemetry import CollectorTelemetry
from contracts.sample_batch import (
    DEFAULT_MAX_BATCH_BYTES,
    DEFAULT_MAX_SAMPLES_PER_BATCH,
    SampleBatch,
)
from core.application import ProtocolSample

logger = logging.getLogger(__name__)


class CollectorPublisher:
    """Publish bounded immutable batches to Core NATS or JetStream."""

    # Keep a short per-(device, point) history of transport-confirmed samples
    # instead of a global tail. A global tail rotates a Sink-subscribed point
    # out under wide point fan-out, and a single latest entry loses the race
    # against the next batch; bounded per-point history keeps correlation
    # provable while the diagnostic payload stays small.
    _EVIDENCE_HISTORY = 8
    _EVIDENCE_MAX_KEYS = 256

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
        self._delivery: Literal["realtime", "reliable"] = (
            "realtime" if delivery == "realtime" else "reliable"
        )
        self._telemetry = telemetry
        self._builder = BatchBuilder(
            site_id=site_id,
            collector_id=collector_id,
            delivery=self._delivery,
            max_staleness_ms=max_staleness_ms,
            max_samples_per_batch=max_samples_per_batch,
            max_batch_bytes=max_batch_bytes,
        )
        self._max_samples = max_samples_per_batch
        self._max_bytes = max_batch_bytes
        self._enqueue_timeout_s = enqueue_timeout_s
        self._queue: asyncio.Queue[SampleBatch] = asyncio.Queue(maxsize=capacity)
        self._admission_lock = asyncio.Lock()
        self._queue_space = asyncio.Event()
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
        self.attempted_samples = 0
        self.published_samples = 0
        self.pubacked_samples = 0
        self._last_sample_evidence: tuple[dict[str, str], ...] = ()

    @property
    def last_sample_evidence(self) -> tuple[dict[str, str], ...]:
        """返回按设备可过滤的有界传输确认证据，而非全局匿名 ID。"""
        return self._last_sample_evidence

    @property
    def last_sample_ids(self) -> tuple[str, ...]:
        """Return identities for existing metrics consumers."""
        return tuple(item["sample_id"] for item in self._last_sample_evidence)

    @property
    def connected(self) -> bool:
        return bool(self._connection and self._connection.is_connected)

    @property
    def delivery_mode(self) -> str:
        return self._delivery

    @property
    def collector_id(self) -> str:
        """Return stable producer identity."""
        return self._builder.collector_id

    def update_site(self, site_id: str) -> None:
        """Change the site identity for subsequent batches only."""
        self._builder.update_site(site_id)

    async def start(self) -> None:
        """Launch exactly one ordered publisher loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def validate_encoding(
        self,
        device_id: str,
        sample: ProtocolSample,
        *,
        device_type: str,
        point_table_id: str,
    ) -> None:
        """Validate the actual v3 transport schema without sending it."""
        self._builder.pack(
            device_id, (sample,), device_type=device_type, point_table_id=point_table_id
        )

    async def publish(
        self,
        device_id: str,
        samples: tuple[ProtocolSample, ...],
        *,
        device_type: str,
        point_table_id: str,
    ) -> None:
        """Queue each complete source batch; reject invalid or duplicate points."""
        if not samples:
            return
        try:
            batches = self._builder.pack(
                device_id,
                samples,
                device_type=device_type,
                point_table_id=point_table_id,
            )
        except (ValueError, TypeError) as exc:
            self.last_error = str(exc)
            self.failed += 1
            self.not_attempted += 1
            if self._telemetry is not None:
                self._telemetry.increment("collector.publisher.failed")
            # Invalid samples must not be reported as an accepted acquisition
            # batch. The owning task reports the failure and can recover.
            raise ValueError(f"acquisition batch rejected: {exc}") from exc
        if self._delivery == "reliable":
            # Never publish an accepted prefix of an event later reported as
            # rejected. Queue admission is all-or-nothing for its fragments.
            async with self._admission_lock:
                if len(batches) > self._queue.maxsize:
                    self.not_attempted += len(batches)
                    self.failed += 1
                    raise RuntimeError("reliable acquisition event exceeds bounded queue capacity")
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self._enqueue_timeout_s
                while self._queue.qsize() + len(batches) > self._queue.maxsize:
                    self._queue_space.clear()
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        self.last_error = "reliable event admission timed out"
                        self.backpressure += 1
                        self.not_attempted += len(batches)
                        self.failed += 1
                        raise RuntimeError(
                            "reliable event enqueue timed out; publication not accepted"
                        )
                    try:
                        await asyncio.wait_for(self._queue_space.wait(), remaining)
                    except TimeoutError as exc:
                        self.last_error = "reliable event admission timed out"
                        self.backpressure += 1
                        self.not_attempted += len(batches)
                        self.failed += 1
                        raise RuntimeError(
                            "reliable event enqueue timed out; publication not accepted"
                        ) from exc
                for batch in batches:
                    self._queue.put_nowait(batch)
            return
        for batch in batches:
            try:
                self._queue.put_nowait(batch)
            except asyncio.QueueFull:
                self.last_error = "batch queue capacity exhausted"
                self.backpressure += 1
                self.not_attempted += 1
                self.dropped += 1
                if self._telemetry is not None:
                    self._telemetry.increment("collector.publisher.backpressure")
                    self._telemetry.increment("collector.publisher.dropped")
                    self._telemetry.event(
                        "WARNING",
                        "nats",
                        "BACKPRESSURE",
                        f"batch queue full for {device_id}",
                    )

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
        payload = batch.encode(max_samples=self._max_samples, max_bytes=self._max_bytes)
        subject = batch.subject()
        self.attempted += 1
        self.attempted_samples += len(batch.samples)
        if self._telemetry is not None:
            self._telemetry.increment("collector.publisher.attempted")
            self._telemetry.increment("collector.publisher.samples_attempted", len(batch.samples))
        if self._delivery == "realtime":
            await connection.publish(subject, payload)
            self.client_submitted += 1
            if self._telemetry is not None:
                self._telemetry.increment("collector.publisher.client_submitted")
        else:
            # Retries, if initiated by the broker or an operator, retain the
            # original batch_id. PubAck is the only publication confirmation.
            await connection.jetstream().publish(
                subject,
                payload,
                timeout=2,
                headers={"Nats-Msg-Id": batch.batch_id},
            )
            self.pubacked += 1
            self.pubacked_samples += len(batch.samples)
            if self._telemetry is not None:
                self._telemetry.increment("collector.publisher.pubacked")
                self._telemetry.increment(
                    "collector.publisher.samples_pubacked", len(batch.samples)
                )
        self.published += 1
        self.published_samples += len(batch.samples)
        confirmed_at = datetime.now(UTC).isoformat()
        evidence: dict[tuple[str, str], deque[dict[str, str]]] = {}
        for item in self._last_sample_evidence:
            key = (item["device_id"], item["point_id"])
            evidence.setdefault(key, deque(maxlen=self._EVIDENCE_HISTORY)).append(item)
        for sample in batch.samples:
            key = (batch.device_id, sample.point_id)
            history = evidence.get(key)
            if history is None:
                history = deque(maxlen=self._EVIDENCE_HISTORY)
                evidence[key] = history
            history.append(
                {
                    "device_id": batch.device_id,
                    "point_id": sample.point_id,
                    "sample_id": sample.sample_id,
                    "confirmed_at": confirmed_at,
                }
            )
        overflow = len(evidence) - self._EVIDENCE_MAX_KEYS
        if overflow > 0:
            for stale_key in tuple(evidence)[:overflow]:
                del evidence[stale_key]
        self._last_sample_evidence = tuple(
            entry for history in evidence.values() for entry in history
        )
        if self._telemetry is not None:
            self._telemetry.increment("collector.publisher.samples_published", len(batch.samples))
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
            self._queue_space.set()
            # One source acquisition event remains one transport batch (or
            # capacity-limited fragments). Stamp once at first send attempt;
            # JetStream retries preserve exactly this identity and timestamp.
            batch = batch.model_copy(update={"publish_time": datetime.now(UTC)})
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
