"""Reliable Delivery Application state machine and terminal batch decisions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, cast

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.application.latency_window import LatencyWindow
from sink_service.application.ports.reliable import ReliableMessagePort
from sink_service.application.realtime import BatchOutcome
from sink_service.application.reliable_policy import classify_settlement, select_reliable
from sink_service.domain.config import ValidatedSnapshot

logger = logging.getLogger(__name__)


class ReliableDeliveryEngine:
    """One Delivery owns source acknowledgement, retries, DLQ and target settlement."""

    _definition: DeliveryDefinition
    _snapshot: ValidatedSnapshot
    _stop: asyncio.Event
    _adapter: Any
    _kafka_target: bool
    _data_level: str
    _active_io: bool
    _capacity_alarm: str
    _last_capacity_check: float
    _last_backlog_progress: float
    _last_backlog_output: float
    _backlog: int | None
    received_total: int
    received_batches_total: int
    acknowledged_batches_total: int
    rejected_batches_total: int
    input_values_total: int
    delivered_total: int
    failed_total: int
    write_success_total: int
    retry_total: int
    dlq_total: int
    dlq_invalid_batches_total: int
    filtered_ack_batches_total: int
    filtered_ack_values_total: int
    _pending_filter_reason: str
    _pending_confirmed_samples: list[dict[str, str]]
    _confirmed_samples: deque[dict[str, str]]
    latency_window: LatencyWindow
    reliable_source_fetch_seconds_total: float
    reliable_source_fetch_operations_total: int
    reliable_encode_seconds_total: float
    reliable_encode_operations_total: int
    reliable_write_seconds_total: float
    reliable_write_operations_total: int
    reliable_source_ack_seconds_total: float
    reliable_source_ack_operations_total: int
    kafka_lock_seconds_total: float
    kafka_metadata_seconds_total: float
    kafka_submit_seconds_total: float
    kafka_confirm_seconds_total: float
    kafka_timed_writes_total: int
    quota_global_seconds_total: float
    quota_physical_seconds_total: float
    quota_native_seconds_total: float
    quota_timed_writes_total: int
    last_error: str

    async def _fetch_messages(self) -> list[ReliableMessagePort]:
        """Implemented by the NATS source adapter."""
        raise NotImplementedError

    async def _dead_letter(
        self, message: ReliableMessagePort, reason: str, *, sample: DeliveryPoint | None = None
    ) -> None:
        """Implemented by the JetStream DLQ adapter."""
        raise NotImplementedError

    async def _check_capacity(self) -> None:
        """Implemented by the JetStream metadata adapter."""
        raise NotImplementedError

    async def _encode_kafka_points(
        self, batch: SampleBatch, items: tuple[SampleItem, ...]
    ) -> tuple[bytes, ...]:
        """Implemented by the Kafka output serialization adapter."""
        raise NotImplementedError

    async def _dlq_with_heartbeat(
        self,
        message: ReliableMessagePort,
        reason: str,
        *,
        sample: DeliveryPoint | None = None,
    ) -> None:
        """Keep the source consumer lease alive during slow durable DLQ transfer."""
        transfer = asyncio.create_task(self._dead_letter(message, reason, sample=sample))
        interval = max(0.5, self._snapshot.service["reliable"]["ack_wait_seconds"] / 3)
        while not transfer.done():
            try:
                await asyncio.wait_for(asyncio.shield(transfer), timeout=interval)
            except TimeoutError:
                try:
                    await message.extend()
                except Exception:
                    logger.warning("DLQ source ACK extension failed", exc_info=True)
        await asyncio.shield(transfer)

    async def _encode_with_heartbeat(
        self,
        message: ReliableMessagePort,
        batch: SampleBatch,
        items: tuple[SampleItem, ...],
    ) -> tuple[bytes, ...]:
        """Keep source ownership while a historical mapping is fetched and verified."""
        task = asyncio.create_task(self._encode_kafka_points(batch, items))
        interval = max(0.5, self._snapshot.service["reliable"]["ack_wait_seconds"] / 3)
        try:
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=interval)
                except TimeoutError:
                    try:
                        await message.extend()
                    except Exception:
                        logger.warning("source ACK extension during mapping failed", exc_info=True)
            return await asyncio.shield(task)
        finally:
            # Unlike an already submitted target write, a pending read-only
            # metadata query can be cancelled safely on Delivery shutdown.
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _write_with_heartbeat(
        self,
        message: Any,
        samples: tuple[DeliveryPoint, ...] | tuple[bytes, ...],
        *,
        key: bytes | None = None,
        stripe: int = 0,
    ) -> BatchOutcome:
        """Keep native target I/O owned even if the AckWait renewal fails."""
        assert self._adapter is not None

        async def native_write() -> BatchOutcome:
            if key is None:
                result: BatchOutcome = await self._adapter.write_batch(samples)
            else:
                result = await self._adapter.write_encoded_batch(samples, key=key, stripe=stripe)
            if result.error == "Sink I/O result uncertain; waiting for settlement":
                settled = await self._adapter.settle()
                return settled if settled is not None else result
            return result

        task = asyncio.create_task(native_write())
        interval = max(0.5, self._snapshot.service["reliable"]["ack_wait_seconds"] / 3)
        progress_error: Exception | None = None
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except TimeoutError:
                try:
                    await message.extend()
                except Exception as exc:
                    progress_error = exc
                continue
        result: BatchOutcome = await asyncio.shield(task)
        if progress_error is not None:
            # A failed InProgress signal does not invalidate a settled target
            # write. Attempt the source ACK using that actual result; Broker
            # redelivery remains possible if the consumer lease was lost.
            logger.warning(
                "JetStream ACK extension failed for Delivery %s: %s",
                self._definition.delivery_id,
                progress_error,
            )
        return result

    async def _retry_pause(self, seconds: float) -> None:
        """A STOP must not wait for the full transient backoff."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _deliver_batch(self, message: ReliableMessagePort, batch: SampleBatch) -> bool:
        """Return true only after all matched points reach a durable terminal state."""
        selector = self._snapshot.deliveries[self._definition.delivery_id]["source"]
        selection = select_reliable(batch, selector, kafka=self._kafka_target)
        selected = selection.items
        samples = selection.points
        key = selection.key
        stripe = selection.stripe
        count = selection.count
        self._pending_filter_reason = selection.filter_reason
        self._pending_confirmed_samples = []
        if not count:
            return True
        # RAW requires original source bytes for every selected sample. Reject
        # the complete source batch before any Kafka write so a later invalid
        # point cannot create an accepted prefix or an infinite redelivery loop.
        if self._kafka_target and self._data_level == "raw":
            for item in selected:
                if item.quality == "not_collected":
                    continue
                raw = item.raw_payload_hex
                if (
                    not isinstance(raw, str)
                    or not 2 <= len(raw) <= 4096
                    or len(raw) % 2 != 0
                    or any(char not in "0123456789abcdef" for char in raw)
                ):
                    await self._dlq_with_heartbeat(
                        message,
                        f"RAW source payload absent or invalid: {item.point_id}",
                    )
                    self.rejected_batches_total += 1
                    return True
        self.received_total += count
        assert self._adapter is not None
        unresolved = False
        batch_step = count if self._kafka_target else 500
        for start in range(0, count, batch_step):
            if start:
                try:
                    await message.extend()
                except Exception:
                    logger.warning("source ACK extension between subbatches failed", exc_info=True)
            if self._stop.is_set():
                return False
            payloads: tuple[bytes, ...] = ()
            chunk: tuple[DeliveryPoint, ...] = ()
            if self._kafka_target:
                chunk_items = selected[start : start + batch_step]
                encode_started = time.monotonic()
                try:
                    payloads = await self._encode_with_heartbeat(message, batch, chunk_items)
                finally:
                    self.reliable_encode_seconds_total += time.monotonic() - encode_started
                    self.reliable_encode_operations_total += 1
                chunk_length = len(payloads)
            else:
                chunk = samples[start : start + 500]
                chunk_length = len(chunk)
            self._active_io = True
            write_started = time.monotonic()
            try:
                if self._kafka_target:
                    outcome = await self._write_with_heartbeat(
                        message, payloads, key=key, stripe=stripe
                    )
                else:
                    outcome = await self._write_with_heartbeat(message, chunk)
            finally:
                self._active_io = False
                self.reliable_write_seconds_total += time.monotonic() - write_started
                self.reliable_write_operations_total += 1
            if self._kafka_target and outcome.kafka_stage_seconds is not None:
                lock_s, metadata_s, submit_s, confirm_s = outcome.kafka_stage_seconds
                self.kafka_lock_seconds_total += lock_s
                self.kafka_metadata_seconds_total += metadata_s
                self.kafka_submit_seconds_total += submit_s
                self.kafka_confirm_seconds_total += confirm_s
                self.kafka_timed_writes_total += 1
            if outcome.quota_stage_seconds is not None:
                global_s, physical_s, native_s = outcome.quota_stage_seconds
                self.quota_global_seconds_total += global_s
                self.quota_physical_seconds_total += physical_s
                self.quota_native_seconds_total += native_s
                self.quota_timed_writes_total += 1
            decision = classify_settlement(outcome, chunk_length, kafka=self._kafka_target)
            # Preserve only target-confirmed identities. Publish to diagnostics
            # after the corresponding source ACK, never on an uncertain write.
            for index in outcome.delivered:
                item = selected[0] if self._kafka_target else cast(SampleItem, chunk[index])
                self._pending_confirmed_samples.append(
                    {
                        "device_id": batch.device_id,
                        "sample_id": item.sample_id,
                        "delivery_id": self._definition.delivery_id,
                        "channel": "reliable",
                    }
                )
                if len(self._pending_confirmed_samples) > 4:
                    self._pending_confirmed_samples.pop(0)
            self.write_success_total += outcome.successful_attempts
            self.failed_total += outcome.failed_attempts
            self.delivered_total += decision.delivered
            timestamp = datetime.now(UTC)
            for latency in outcome.successful_latencies_ms:
                self.latency_window.add(timestamp, latency)
            for latency, count in outcome.successful_latency_weights:
                self.latency_window.add(timestamp, latency, count=count)
            for index in decision.permanent:
                await self._dlq_with_heartbeat(
                    message,
                    outcome.error or "permanent target business rejection",
                    sample=samples[start + index],
                )
            if decision.unresolved:
                unresolved = True
            if unresolved:
                break
        return not unresolved

    async def _run(self) -> None:
        """Process one durable source batch at a time; never ACK partial results."""
        config = self._snapshot.service["reliable"]
        base_backoff = config["retry_base_ms"] / 1000
        max_backoff = config["retry_max_ms"] / 1000
        backoff = base_backoff
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_capacity_check >= 10:
                self._last_capacity_check = now
                await self._check_capacity()
            fetch_started = time.monotonic()
            try:
                messages = await self._fetch_messages()
            except TimeoutError:
                continue
            except Exception as exc:
                self.last_error = str(exc)
                self.retry_total += 1
                await self._retry_pause(min(backoff, max_backoff))
                backoff = min(backoff * 2, max_backoff)
                continue

            if messages:
                self.reliable_source_fetch_seconds_total += time.monotonic() - fetch_started
                self.reliable_source_fetch_operations_total += len(messages)
            for message in messages:
                if self._stop.is_set():
                    return
                metadata = message.metadata
                self._backlog = metadata.num_pending + 1
                self.received_batches_total += 1
                try:
                    batch = SampleBatch.decode(
                        message.data,
                        mode="reliable",
                        subject=message.subject,
                        max_samples=self._snapshot.service["nats"]["max_samples_per_batch"],
                        max_bytes=self._snapshot.service["nats"]["max_batch_bytes"],
                    )
                except (ValueError, UnicodeError) as exc:
                    self.rejected_batches_total += 1
                    try:
                        await self._dlq_with_heartbeat(message, str(exc))
                        await message.ack(timeout=5)
                        self.acknowledged_batches_total += 1
                        self._backlog = metadata.num_pending
                        self._last_backlog_progress = time.monotonic()
                    except Exception as failure:
                        self.last_error = f"DLQ_BLOCKED: {failure}"
                        self.retry_total += 1
                        try:
                            await message.retry(delay=min(backoff, max_backoff))
                        except Exception:
                            logger.warning("NAK after blocked DLQ failed", exc_info=True)
                            await self._retry_pause(min(backoff, max_backoff))
                        backoff = min(backoff * 2, max_backoff)
                    continue

                self.input_values_total += len(batch.samples)
                historical = (
                    metadata.num_delivered > 1
                    or (datetime.now(UTC) - metadata.timestamp).total_seconds() > 5
                )
                if historical:
                    rate = config["replay_max_rate_per_second"]
                    now = asyncio.get_running_loop().time()
                    delay = self._last_backlog_output + 1 / rate - now
                    if delay > 0:
                        await self._retry_pause(delay)
                    self._last_backlog_output = asyncio.get_running_loop().time()
                if self._stop.is_set():
                    return
                try:
                    # Message headers do not establish Delivery ownership.
                    # Only the validated source selector may skip a source batch.
                    terminal = await self._deliver_batch(message, batch)
                    if not terminal:
                        raise RuntimeError("source batch contains unconfirmed points")
                    ack_started = time.monotonic()
                    try:
                        await message.ack(timeout=5)
                    finally:
                        self.reliable_source_ack_seconds_total += time.monotonic() - ack_started
                        self.reliable_source_ack_operations_total += 1
                    self.acknowledged_batches_total += 1
                    self._confirmed_samples.extend(self._pending_confirmed_samples[-4:])
                    self._pending_confirmed_samples.clear()
                    if self._pending_filter_reason:
                        self.filtered_ack_batches_total += 1
                        self.filtered_ack_values_total += len(batch.samples)
                        logger.info(
                            "Reliable zero-match ACK delivery=%s revision=%s reason=%s "
                            "batch_values=%d",
                            self._definition.delivery_id,
                            self._snapshot.revision,
                            self._pending_filter_reason,
                            len(batch.samples),
                        )
                    self._pending_filter_reason = ""
                    self._backlog = metadata.num_pending
                    self._last_backlog_progress = time.monotonic()
                    self.last_error = ""
                    backoff = base_backoff
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Durable redelivery is the only recovery path. Confirmed
                    # subsets must be idempotent downstream on replay.
                    self.retry_total += 1
                    self.last_error = str(exc)
                    logger.warning(
                        "Reliable Delivery %s failed; source batch remains unacknowledged: %s",
                        self._definition.delivery_id,
                        type(exc).__name__,
                    )
                    try:
                        await message.retry(delay=min(backoff, max_backoff))
                    except Exception as nak_error:
                        self.last_error = f"{exc}; NAK unresolved: {nak_error}"
                        await self._retry_pause(min(backoff, max_backoff))
                    backoff = min(backoff * 2, max_backoff)
