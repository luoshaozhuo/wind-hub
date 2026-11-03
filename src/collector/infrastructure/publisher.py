"""Collector NATS publisher using the authoritative sample transport schema."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

import nats
from nats.aio.client import Client

from collector.application.telemetry import CollectorTelemetry
from contracts.sample_message import SampleMessage, validate_subject_token
from core.application import ProtocolSample

logger = logging.getLogger(__name__)


class CollectorPublisher:
    """Bounded realtime or acknowledged JetStream sample publishing."""

    def __init__(
        self,
        *,
        servers: list[str],
        site_id: str,
        collector_id: str,
        delivery: str,
        max_staleness_ms: int = 500,
        capacity: int = 2048,
        telemetry: CollectorTelemetry | None = None,
    ) -> None:
        if delivery not in {"realtime", "reliable"}:
            raise ValueError("unsupported delivery mode")
        self._servers = servers
        self._site_id = validate_subject_token(site_id)
        self._collector_id = validate_subject_token(collector_id)
        # Narrow after validation so SampleMessage.subject() receives its
        # Literal channel without a cast at the call site.
        self._delivery: Literal["realtime", "reliable"] = (
            "realtime" if delivery == "realtime" else "reliable"
        )
        self._telemetry = telemetry
        self._staleness = max_staleness_ms
        self._queue: asyncio.Queue[SampleMessage] = asyncio.Queue(maxsize=capacity)
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

    def update_site(self, site_id: str) -> None:
        self._site_id = validate_subject_token(site_id)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def _message(self, device_id: str, sample: ProtocolSample) -> SampleMessage:
        return SampleMessage(
            wind_farm_id=self._site_id,
            collector_id=self._collector_id,
            task_id=device_id,
            device_id=device_id,
            point_id=sample.point_id,
            sample_id=uuid.uuid4().hex,
            value=sample.value,
            quality=sample.quality.value,
            sample_time=sample.timestamp,
            receive_time=sample.receive_time,
            publish_time=datetime.now(UTC),
            max_staleness_ms=(self._staleness if self._delivery == "realtime" else None),
        )

    def validate_encoding(self, device_id: str, sample: ProtocolSample) -> None:
        """Validate the exact transport envelope without publishing it."""
        self._message(device_id, sample).encode()

    async def publish(self, device_id: str, samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            try:
                message = self._message(device_id, sample)
                self._queue.put_nowait(message)
            except asyncio.QueueFull:
                self.last_error = "sample queue capacity exhausted"
                if self._telemetry is not None:
                    self._telemetry.event(
                        "WARNING",
                        "nats",
                        "BACKPRESSURE",
                        f"sample queue full for {device_id}:{sample.point_id}",
                    )
                self.backpressure += 1
                self.not_attempted += 1
                self.dropped += 1
                if self._telemetry is not None:
                    self._telemetry.increment("collector.publisher.backpressure")
                    self._telemetry.increment("collector.publisher.dropped")
            except ValueError:
                self.failed += 1
                self.not_attempted += 1
                if self._telemetry is not None:
                    self._telemetry.increment("collector.publisher.failed")

    async def _run(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                if self._delivery == "realtime":
                    age_ms = (datetime.now(UTC) - message.sample_time).total_seconds() * 1000
                    if age_ms > self._staleness:
                        self.expired += 1
                        self.dropped += 1
                        self.not_attempted += 1
                        if self._telemetry is not None:
                            self._telemetry.increment("collector.publisher.expired")
                            self._telemetry.increment("collector.publisher.dropped")
                        continue
                if self._connection is None or not self._connection.is_connected:
                    try:
                        if self._connection is not None:
                            await self._connection.close()
                        self._connection = await nats.connect(
                            servers=self._servers,
                            connect_timeout=2,
                            max_reconnect_attempts=3,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.last_error = str(exc)
                        self._connection = None
                        # The sample never reached a publish attempt. Count
                        # exactly one dropped outcome, with not_attempted as
                        # an orthogonal diagnostic attribute.
                        self.dropped += 1
                        self.not_attempted += 1
                        self.connection_failed += 1
                        if self._telemetry is not None:
                            self._telemetry.increment("collector.publisher.dropped")
                            self._telemetry.increment("collector.publisher.connection_failed")
                        logger.warning("Collector NATS connection failed: %s", exc)
                        if self._telemetry is not None:
                            self._telemetry.event("ERROR", "nats", "CONNECT_FAILED", str(exc))
                        await asyncio.sleep(0.2)
                        continue
                fresh = message.model_copy(update={"publish_time": datetime.now(UTC)})
                try:
                    subject = fresh.subject(self._delivery)
                    payload = fresh.encode()
                except (TypeError, ValueError) as exc:
                    # A local encoding failure is known not to have been sent.
                    self.last_error = str(exc)
                    self.failed += 1
                    self.not_attempted += 1
                    if self._telemetry is not None:
                        self._telemetry.increment("collector.publisher.failed")
                    continue
                self.attempted += 1
                if self._telemetry is not None:
                    self._telemetry.increment("collector.publisher.attempted")
                if self._delivery == "realtime":
                    await self._connection.publish(subject, payload)
                    # Local client acceptance is not a server-side confirmation;
                    # never report it under the PubAck-confirmed counter.
                    self.client_submitted += 1
                    if self._telemetry is not None:
                        self._telemetry.increment("collector.publisher.client_submitted")
                else:
                    jetstream = self._connection.jetstream()
                    await jetstream.publish(
                        subject,
                        payload,
                        timeout=2,
                    )
                    self.pubacked += 1
                    if self._telemetry is not None:
                        self._telemetry.increment("collector.publisher.pubacked")
                self.published += 1
                if self._telemetry is not None:
                    # receive_time is optional; a driver that only stamps the
                    # sample time falls back to it for the latency baseline.
                    received_at = message.receive_time or message.sample_time
                    self._telemetry.observe_milliseconds(
                        "collector.publisher.latency_ms",
                        max(0.0, (datetime.now(UTC) - received_at).total_seconds() * 1000),
                    )
                self.last_error = ""
            except asyncio.CancelledError:
                # A cancelled in-flight send has an unknown remote outcome.
                # Do not count a cancellation while idle as a publish attempt.
                if self.attempted > self.published + self.unconfirmed:
                    self.unconfirmed += 1
                    if self._telemetry is not None:
                        self._telemetry.increment("collector.publisher.unconfirmed")
                raise
            except Exception as exc:
                self.last_error = str(exc)
                # Unknown PubAck means delivery may have succeeded; never claim
                # confirmed loss or re-publish blindly.
                self.unconfirmed += 1
                if self._telemetry is not None:
                    self._telemetry.increment("collector.publisher.unconfirmed")
                logger.warning("Collector publish unconfirmed: %s", exc)
                if self._telemetry is not None:
                    self._telemetry.event("WARNING", "nats", "PUBLISH_UNCONFIRMED", str(exc))
                await asyncio.sleep(0.2)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        if self._task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=2)
            except TimeoutError:
                # Drain queued, unsent messages so queue.join() remains usable
                # on a later start and no sample is silently retained.
                discarded = 0
                while True:
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._queue.task_done()
                    discarded += 1
                self.dropped += discarded
                self.not_attempted += discarded
                if self._telemetry is not None and discarded:
                    self._telemetry.increment("collector.publisher.dropped", discarded)
                    self._telemetry.event(
                        "WARNING",
                        "nats",
                        "SHUTDOWN_DROPPED",
                        f"{discarded} queued messages discarded during shutdown",
                    )
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
