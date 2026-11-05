"""JetStream reliable Delivery: durable processing, bounded retries and DLQ ownership."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

import nats
from nats.aio.client import Client
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig

from contracts.sample_batch import BatchSample, SampleBatch
from contracts.sample_message import SampleMessage
from sink_service.application.config import template_document as _template
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot
from sink_service.infrastructure.realtime_runtime import SinkOutputPool

logger = logging.getLogger(__name__)


def _js_name(value: Any) -> str:
    """Normalize NATS JetStream config values represented by str or Enum."""
    if isinstance(value, str):
        return value.lower()
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name.lower()
    raise ConfigurationError("unrecognized JetStream configuration value")


class ReliableDeliveryPort:
    """One durable JetStream Consumer and one in-flight target write per Delivery."""

    def __init__(
        self,
        definition: DeliveryDefinition,
        snapshot: ValidatedSnapshot,
        pool: SinkOutputPool,
    ) -> None:
        self._pool = pool
        self._definition = definition
        self._snapshot = snapshot
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._client: Client | None = None
        self._subscription: Any = None
        self._adapter: Any = None
        self.last_error = ""
        self.received_total = 0
        self.received_batches_total = 0
        self.acknowledged_batches_total = 0
        self.rejected_batches_total = 0
        self.input_values_total = 0
        self.delivered_total = 0
        self._backlog: int | None = None
        self._active_io = False
        self._last_backlog_output = 0.0
        self.failed_total = 0
        self.write_success_total = 0
        self.latency_samples: deque[tuple[datetime, float]] = deque(maxlen=50000)
        self.latency_overflow_at: datetime | None = None
        self.retry_total = 0
        self.dlq_total = 0
        self.dlq_invalid_batches_total = 0
        self._started_at = datetime.now(UTC)
        self._capacity_alarm = ''
        self._last_capacity_check = 0.0
        self._last_backlog_progress = time.monotonic()

    @property
    def last_error(self) -> str:
        task = self._task
        if task is not None and not task.done() and (
            self._client is None or not self._client.is_connected
        ):
            return 'JetStream source disconnected'
        if self._capacity_alarm:
            return self._capacity_alarm
        if (
            task is not None and not task.done() and self._backlog is not None
            and self._backlog > 0
            and time.monotonic() - self._last_backlog_progress
            >= self._snapshot.service['reliable']['replay_starvation_alarm_seconds']
        ):
            return 'REPLAY_STARVATION: backlog did not progress'
        return self._last_error

    @last_error.setter
    def last_error(self, value: str) -> None:
        self._last_error = value
    def inspect(self) -> dict[str, str]:
        """Report known JetStream and target state without a new probe."""
        return {
            'source': (
                'CONNECTED' if self._client is not None and self._client.is_connected
                else 'DISCONNECTED'
            ),
            'consumer': 'BOUND' if self._subscription is not None else 'NOT_BOUND',
            'delivery': (
                'RUNNING' if self._task is not None and not self._task.done()
                else 'STOPPED'
            ),
            'sink': 'OWNED' if self._adapter is not None else 'NOT_ATTACHED',
            'io_inflight': str(self._active_io),
            'target': 'NOT_PROBED',
        }
    @property
    def metrics(self) -> dict[str, float]:
        values = {
            'received_total': float(self.received_total),
            'input_batches_total': float(self.received_batches_total),
            'input_values_total': float(self.input_values_total),
            'acknowledged_batches_total': float(self.acknowledged_batches_total),
            'rejected_batches_total': float(self.rejected_batches_total),
            'delivered_total': float(self.delivered_total),
            'failed_total': float(self.failed_total),
            'write_success_total': float(self.write_success_total),
            'dead_letter_total': float(self.dlq_total),
            'dead_letter_invalid_batches_total': float(self.dlq_invalid_batches_total),
            'retry_total': float(self.retry_total),
            'io_inflight': float(self._active_io),
        }
        if (
            self._backlog is not None and self._client is not None
            and self._client.is_connected
        ):
            values['backlog_messages'] = float(self._backlog)
        return values

    def _validate_target(self) -> None:
        sink = self._snapshot.sinks[self._definition.sink_id]
        template = _template(
            self._snapshot, 'sinks', sink['template_id'], sink['template_version']
        )
        caps = template['capabilities']
        if not caps['reliable']:
            raise ConfigurationError('Sink does not declare Reliable capability')
        kind = template['kind']
        if kind == 'redis':
            if not caps['idempotent']:
                raise ConfigurationError(
                    'Reliable Redis must declare atomic idempotent version protection'
                )
        elif kind == 'kafka':
            output = template['output']
            policy = output['business_duplicate_policy']
            if policy not in ('require_dedup', 'allow_at_least_once'):
                raise ConfigurationError('Reliable Kafka duplicate policy missing')
            if policy == 'require_dedup':
                identity = output.get('dedup_identity_fields')
                if (
                    not isinstance(identity, list)
                    or len(identity) != 2
                    or any(not isinstance(field, str) for field in identity)
                    or set(identity) != {'delivery_id', 'sample_id'}
                ):
                    raise ConfigurationError(
                        'Reliable Kafka require_dedup needs delivery_id + sample_id'
                    )
                # This validates the declared downstream responsibility only.
                # Kafka Producer ACK does not prove consumer-side deduplication.
        else:
            raise ConfigurationError('unsupported Reliable Sink Adapter')
    @staticmethod
    def _durable_id(snapshot: ValidatedSnapshot, delivery_id: str) -> str:
        seed = f"{snapshot.service['deployment_id']}:{delivery_id}".encode("utf-8")
        return "WH_SINK_" + hashlib.sha256(seed).hexdigest()[:24]

    async def start(self) -> None:
        if self._task is not None:
            if self._task.done():
                raise ConfigurationError('prior Reliable consumer needs safe STOP before START')
            return
        self._validate_target()
        adapter = await self._pool.acquire(self._definition.sink_id, self._snapshot)
        self._adapter = adapter
        config = self._snapshot.service
        nats_config = config["nats"]
        try:
            client = await nats.connect(
                servers=nats_config["servers"],
                allow_reconnect=True,
                max_reconnect_attempts=-1,
                connect_timeout=nats_config["connect_timeout_ms"] / 1000,
                reconnect_time_wait=nats_config["max_reconnect_interval_ms"] / 1000,
            )
            self._client = client
            js = client.jetstream()
            stream = await js.stream_info(nats_config["reliable_stream"])
            if not any(
                subject == nats_config["reliable_subject"] + ".>"
                for subject in stream.config.subjects
            ):
                raise ConfigurationError("history Stream does not cover sample subjects")
            if _js_name(stream.config.storage) != "file":
                raise ConfigurationError("history Stream must use file storage")
            if _js_name(stream.config.discard) != "new":
                raise ConfigurationError("history Stream must reject overflow")
            if (
                _js_name(stream.config.retention) != 'limits'
                or stream.config.max_bytes <= 0
                or stream.config.max_age <= 0
            ):
                raise ConfigurationError('history Stream requires finite Limits retention')
            dead = await js.stream_info(nats_config['dead_letter_stream'])
            if nats_config['dead_letter_subject'] not in dead.config.subjects:
                raise ConfigurationError('DLQ Stream subject is not configured')
            if (
                _js_name(dead.config.storage) != 'file'
                or _js_name(dead.config.retention) != 'limits'
                or _js_name(dead.config.discard) != 'new'
                or dead.config.max_bytes <= 0
                or dead.config.max_bytes < nats_config['dlq_max_bytes']
                or dead.config.max_age < nats_config['dlq_max_age_seconds']
            ):
                raise ConfigurationError('DLQ Stream retention/capacity is unsafe')
            source = self._snapshot.deliveries[self._definition.delivery_id]["source"]
            prefix = nats_config["reliable_subject"]
            subject = (
                f"{prefix}.{source['wind_farm_id']}.{source['collector_id']}.>"
            )
            durable = self._durable_id(self._snapshot, self._definition.delivery_id)
            reliable = config["reliable"]
            self._subscription = await js.pull_subscribe(
                subject,
                durable=durable,
                stream=nats_config["reliable_stream"],
                config=ConsumerConfig(
                    durable_name=durable,
                    filter_subject=subject,
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=float(reliable["ack_wait_seconds"]),
                    max_deliver=-1,
                    max_ack_pending=1,
                ),
            )
            actual = await js.consumer_info(nats_config["reliable_stream"], durable)
            if (
                actual.config.max_deliver != -1
                or actual.config.max_ack_pending != 1
                or actual.config.ack_policy != AckPolicy.EXPLICIT
                or actual.config.filter_subject != subject
            ):
                raise ConfigurationError("existing JetStream Durable configuration mismatch")
            self._backlog = actual.num_pending + actual.num_ack_pending
            self._last_backlog_progress = time.monotonic()
            self.last_error = ''
            self._stop.clear()
            task = asyncio.create_task(self._run())
            task.add_done_callback(self._on_task_finished)
            self._task = task
        except BaseException:
            try:
                if self._client is not None:
                    await self._client.close()
            finally:
                self._client = None
                self._subscription = None
                try:
                    await self._pool.release(self._definition.sink_id)
                finally:
                    self._adapter = None
            raise

    def _on_task_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self.last_error = 'Reliable consumer task was cancelled unexpectedly'
            return
        failure = task.exception()
        if failure is not None:
            self.last_error = f'Reliable consumer task stopped unexpectedly: {failure}'
        elif not self._stop.is_set():
            self.last_error = 'Reliable consumer exited without STOP'

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            if task.cancelled():
                self.last_error = 'Reliable consumer task was cancelled'
            else:
                try:
                    # Caller cancellation must not disown a running consumer.
                    await asyncio.shield(task)
                except Exception as exc:
                    self.last_error = f'Reliable consumer terminated: {exc}'
            self._task = None
        client = self._client
        if client is not None:
            await client.close()
            self._client = None
            self._subscription = None
        adapter = self._adapter
        if adapter is not None:
            await adapter.settle()
            await self._pool.release(self._definition.sink_id)
            self._adapter = None

    async def _dead_letter(
        self,
        message: Any,
        reason: str,
        *,
        sample: SampleMessage | None = None,
    ) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("JetStream disconnected")
        config = self._snapshot.service["nats"]
        js = client.jetstream()
        stream = await js.stream_info(config["dead_letter_stream"])
        if config["dead_letter_subject"] not in stream.config.subjects:
            raise ConfigurationError("DLQ Stream does not cover dead-letter subject")
        if _js_name(stream.config.storage) != "file":
            raise ConfigurationError("DLQ Stream must use file storage")
        if _js_name(stream.config.discard) != "new":
            raise ConfigurationError("DLQ Stream must reject overflow")
        metadata = message.metadata
        identity = (
            f'{self._definition.delivery_id}:{metadata.stream}:'
            f'{metadata.consumer}:{metadata.sequence.stream}:'
            f'{sample.sample_id if sample is not None else "invalid-batch"}'
        ).encode('utf-8')
        dead_letter_id = hashlib.sha256(identity).hexdigest()
        try:
            source = json.loads(message.data)
            sample_id = source.get('sample_id', '') if isinstance(source, dict) else ''
        except (UnicodeError, ValueError):
            sample_id = ''
        batch_id = source.get('batch_id', '') if isinstance(source, dict) else ''
        batch_id = source.get("batch_id", "") if isinstance(source, dict) else ""
        if sample is not None:
            sample_id = sample.sample_id
        payload = message.data
        if sample is not None:
            payload = SampleBatch(
                batch_id=batch_id if isinstance(batch_id, str) and batch_id else sample.sample_id,
                mode="reliable",
                wind_farm_id=sample.wind_farm_id,
                collector_id=sample.collector_id,
                task_id=sample.task_id,
                device_id=sample.device_id,
                publish_time=sample.publish_time,
                samples=(BatchSample(
                    point_id=sample.point_id,
                    sample_id=sample.sample_id,
                    value=sample.value,
                    quality=sample.quality,
                    sample_time=sample.sample_time,
                    receive_time=sample.receive_time,
                ),),
            ).encode()
        observed_at = datetime.now(UTC).isoformat()
        # Invalid source batches can approach NATS MaxPayload. Split into
        # independently acknowledged DLQ records; source ACK follows ALL parts.
        chunk_size = 256 * 1024
        chunks = [
            payload[offset : offset + chunk_size]
            for offset in range(0, len(payload), chunk_size)
        ] or [b""]
        new_record = False
        for index, chunk in enumerate(chunks):
            part_id = hashlib.sha256(
                f"{dead_letter_id}:{index}".encode("ascii")
            ).hexdigest()
            record = {
                "dead_letter_id": dead_letter_id,
                "part_id": part_id,
                "part_index": index,
                "part_count": len(chunks),
                "delivery_id": self._definition.delivery_id,
                "sample_id": sample_id[:128] if isinstance(sample_id, str) else "",
                "batch_id": batch_id[:128] if isinstance(batch_id, str) else "",
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "config_revision": self._snapshot.revision,
                "subject": message.subject,
                "source_stream": metadata.stream,
                "consumer": metadata.consumer,
                "source_sequence": metadata.sequence.stream,
                "source_timestamp": metadata.timestamp.astimezone(UTC).isoformat(),
                "payload_base64": base64.b64encode(chunk).decode("ascii"),
                "classification": "PERMANENT",
                "reason": reason[:1024],
                "first_failed_at": observed_at,
                "last_failed_at": observed_at,
            }
            encoded = json.dumps(
                record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            ack = await js.publish(
                config["dead_letter_subject"],
                encoded,
                headers={"Nats-Msg-Id": part_id},
                timeout=5,
            )
            if ack.stream != config["dead_letter_stream"]:
                raise ConfigurationError("DLQ PubAck confirms unexpected Stream")
            if not getattr(ack, "duplicate", False):
                new_record = True
        if new_record:
            if sample is None:
                self.dlq_invalid_batches_total += 1
            else:
                self.dlq_total += 1

    async def _dlq_with_heartbeat(
        self,
        message: Any,
        reason: str,
        *,
        sample: SampleMessage | None = None,
    ) -> None:
        """Keep the source consumer lease alive during slow durable DLQ transfer."""
        transfer = asyncio.create_task(
            self._dead_letter(message, reason, sample=sample)
        )
        interval = max(
            0.5, self._snapshot.service["reliable"]["ack_wait_seconds"] / 3
        )
        while not transfer.done():
            try:
                await asyncio.wait_for(asyncio.shield(transfer), timeout=interval)
            except TimeoutError:
                try:
                    await message.in_progress()
                except Exception:
                    logger.warning("DLQ source ACK extension failed", exc_info=True)
        await asyncio.shield(transfer)

    async def _write_with_heartbeat(
        self, message: Any, samples: tuple[SampleMessage, ...]
    ) -> BatchOutcome:
        """Keep native target I/O owned even if the AckWait renewal fails."""
        assert self._adapter is not None
        async def native_write() -> BatchOutcome:
            result: BatchOutcome = await self._adapter.write_batch(samples)
            if result.error == 'Sink I/O result uncertain; waiting for settlement':
                settled = await self._adapter.settle()
                return settled if settled is not None else result
            return result

        task = asyncio.create_task(native_write())
        interval = max(0.5, self._snapshot.service['reliable']['ack_wait_seconds'] / 3)
        progress_error: Exception | None = None
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except TimeoutError:
                try:
                    await message.in_progress()
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
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _check_capacity(self) -> None:
        """Poll Broker-owned capacity every 10 s; never mutate Streams."""
        client = self._client
        if client is None or not client.is_connected:
            self._capacity_alarm = 'SOURCE_CAPACITY_UNKNOWN: Broker disconnected'
            return
        cfg = self._snapshot.service['nats']
        try:
            js = client.jetstream()
            source = await js.stream_info(cfg['reliable_stream'])
            dead_letter = await js.stream_info(cfg['dead_letter_stream'])
            alarms: list[str] = []
            for label, stream in (('SOURCE', source), ('DLQ', dead_letter)):
                cap = stream.config.max_bytes
                if cap <= 0:
                    alarms.append(f'{label}_CAPACITY_UNKNOWN: no finite MaxBytes')
                elif stream.state.bytes >= cap * 0.9:
                    alarms.append(f'{label}_CAPACITY_RISK: >=90% of MaxBytes')
            self._capacity_alarm = '; '.join(alarms)
        except Exception as exc:
            self._capacity_alarm = f'SOURCE_CAPACITY_UNKNOWN: {exc}'
    async def _deliver_batch(
        self, message: Any, batch: SampleBatch
    ) -> bool:
        """Return true only after all matched points reach a durable terminal state."""
        selector = self._snapshot.deliveries[self._definition.delivery_id]["source"]
        if (
            batch.wind_farm_id != selector["wind_farm_id"]
            or batch.collector_id != selector["collector_id"]
            or (selector["devices"] and batch.device_id not in selector["devices"])
        ):
            return True
        point_ids = frozenset(selector["points"]) if selector["points"] else None
        samples = batch.to_samples(point_ids=point_ids)
        if not samples:
            return True
        self.received_total += len(samples)
        assert self._adapter is not None
        unresolved = False
        for start in range(0, len(samples), 500):
            if start:
                try:
                    await message.in_progress()
                except Exception:
                    logger.warning("source ACK extension between subbatches failed", exc_info=True)
            if self._stop.is_set():
                return False
            chunk = samples[start : start + 500]
            self._active_io = True
            try:
                outcome = await self._write_with_heartbeat(message, chunk)
            finally:
                self._active_io = False
            settled = set(outcome.settled)
            delivered = set(outcome.delivered)
            if (
                not delivered.issubset(settled)
                or len(settled) != len(outcome.settled)
                or any(not 0 <= i < len(chunk) for i in settled)
            ):
                raise RuntimeError("Adapter returned invalid batch indexes")
            self.write_success_total += outcome.successful_attempts
            self.failed_total += outcome.failed_attempts
            self.delivered_total += len(delivered)
            for latency in outcome.successful_latencies_ms:
                timestamp = datetime.now(UTC)
                if len(self.latency_samples) == self.latency_samples.maxlen:
                    self.latency_overflow_at = timestamp
                self.latency_samples.append((timestamp, latency))
            # Adapter-owned, index-based permanent evidence only. An
            # arbitrary error string or an unresolved I/O must never
            # classify a point as DLQ-safe.
            permanent = set(outcome.permanent)
            if (
                len(permanent) != len(outcome.permanent)
                or not permanent.issubset(settled - delivered)
            ):
                raise RuntimeError("Adapter returned invalid permanent indexes")
            for index in sorted(permanent):
                await self._dlq_with_heartbeat(
                    message,
                    outcome.error or "permanent target business rejection",
                    sample=chunk[index],
                )
            if len(delivered | permanent) != len(chunk):
                unresolved = True
            if outcome.error and not permanent:
                unresolved = True
            if unresolved:
                break
        return not unresolved

    async def _run(self) -> None:
        """Process one durable source batch at a time; never ACK partial results."""
        assert self._subscription is not None
        config = self._snapshot.service["reliable"]
        base_backoff = config["retry_base_ms"] / 1000
        max_backoff = config["retry_max_ms"] / 1000
        backoff = base_backoff
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_capacity_check >= 10:
                self._last_capacity_check = now
                await self._check_capacity()
            try:
                messages = await self._subscription.fetch(batch=1, timeout=1.0)
            except (TimeoutError, NatsTimeoutError):
                continue
            except Exception as exc:
                self.last_error = str(exc)
                self.retry_total += 1
                await self._retry_pause(min(backoff, max_backoff))
                backoff = min(backoff * 2, max_backoff)
                continue

            for message in messages:
                if self._stop.is_set():
                    return
                metadata = message.metadata
                self._backlog = metadata.num_pending + 1
                self.received_batches_total += 1
                try:
                    batch = SampleBatch.decode(
                        message.data, mode="reliable", subject=message.subject
                    )
                except (ValueError, UnicodeError) as exc:
                    self.rejected_batches_total += 1
                    try:
                        await self._dlq_with_heartbeat(message, str(exc))
                        await message.ack_sync(timeout=5)
                        self.acknowledged_batches_total += 1
                        self._backlog = metadata.num_pending
                        self._last_backlog_progress = time.monotonic()
                    except Exception as failure:
                        self.last_error = f"DLQ_BLOCKED: {failure}"
                        self.retry_total += 1
                        try:
                            await message.nak(delay=min(backoff, max_backoff))
                        except Exception:
                            logger.warning("NAK after blocked DLQ failed", exc_info=True)
                            await self._retry_pause(min(backoff, max_backoff))
                        backoff = min(backoff * 2, max_backoff)
                    continue

                self.input_values_total += len(batch.samples)
                replay_owner = (message.headers or {}).get("Wh-Replay-Delivery", "")
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
                    terminal = (
                        True if replay_owner and replay_owner != self._definition.delivery_id
                        else await self._deliver_batch(message, batch)
                    )
                    if not terminal:
                        raise RuntimeError("source batch contains unconfirmed points")
                    await message.ack_sync(timeout=5)
                    self.acknowledged_batches_total += 1
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
                    try:
                        await message.nak(delay=min(backoff, max_backoff))
                    except Exception as nak_error:
                        self.last_error = f"{exc}; NAK unresolved: {nak_error}"
                        await self._retry_pause(min(backoff, max_backoff))
                    backoff = min(backoff * 2, max_backoff)


class ReliableFactory:
    """Share one physical target across process-local Durable Delivery owners."""

    def __init__(self) -> None:
        self._pool = SinkOutputPool()

    def __call__(
        self, definition: DeliveryDefinition, snapshot: ValidatedSnapshot
    ) -> ReliableDeliveryPort:
        if definition.channel != "reliable":
            raise ConfigurationError("ReliableFactory cannot own realtime Delivery")
        return ReliableDeliveryPort(definition, snapshot, self._pool)
