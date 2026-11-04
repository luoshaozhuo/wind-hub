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

from contracts.sample_message import SampleMessage
from sink_service.application.config import template_document as _template
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot
from sink_service.infrastructure.realtime_runtime import SinkOutputPool

logger = logging.getLogger(__name__)


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
            'delivered_total': float(self.delivered_total),
            'failed_total': float(self.failed_total),
            'write_success_total': float(self.write_success_total),
            'dead_letter_total': float(self.dlq_total),
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
            if stream.config.storage.name.lower() != "file":
                raise ConfigurationError("history Stream must use file storage")
            if stream.config.discard.name.lower() != "new":
                raise ConfigurationError("history Stream must reject overflow")
            if (
                stream.config.retention.name.lower() != 'limits'
                or stream.config.max_bytes <= 0
                or stream.config.max_age <= 0
            ):
                raise ConfigurationError('history Stream requires finite Limits retention')
            dead = await js.stream_info(nats_config['dead_letter_stream'])
            if nats_config['dead_letter_subject'] not in dead.config.subjects:
                raise ConfigurationError('DLQ Stream subject is not configured')
            if (
                dead.config.storage.name.lower() != 'file'
                or dead.config.retention.name.lower() != 'limits'
                or dead.config.discard.name.lower() != 'new'
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

    def _matches(self, sample: SampleMessage) -> bool:
        source = self._snapshot.deliveries[self._definition.delivery_id]["source"]
        return (
            sample.wind_farm_id == source["wind_farm_id"]
            and sample.collector_id == source["collector_id"]
            and (not source["devices"] or sample.device_id in source["devices"])
            and (not source["points"] or sample.point_id in source["points"])
        )

    async def _dead_letter(self, message: Any, reason: str) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("JetStream disconnected")
        config = self._snapshot.service["nats"]
        js = client.jetstream()
        stream = await js.stream_info(config["dead_letter_stream"])
        if config["dead_letter_subject"] not in stream.config.subjects:
            raise ConfigurationError("DLQ Stream does not cover dead-letter subject")
        if stream.config.storage.name.lower() != "file":
            raise ConfigurationError("DLQ Stream must use file storage")
        if stream.config.discard.name.lower() != "new":
            raise ConfigurationError("DLQ Stream must reject overflow")
        metadata = message.metadata
        identity = (
            f'{self._definition.delivery_id}:{metadata.stream}:'
            f'{metadata.consumer}:{metadata.sequence.stream}'
        ).encode('utf-8')
        dead_letter_id = hashlib.sha256(identity).hexdigest()
        try:
            source = json.loads(message.data)
            sample_id = source.get('sample_id', '') if isinstance(source, dict) else ''
        except (UnicodeError, ValueError):
            sample_id = ''
        observed_at = datetime.now(UTC).isoformat()
        record = {
            'dead_letter_id': dead_letter_id,
            'delivery_id': self._definition.delivery_id,
            'sample_id': sample_id if isinstance(sample_id, str) else '',
            'config_revision': self._snapshot.revision,
            'subject': message.subject,
            'source_stream': metadata.stream,
            'consumer': metadata.consumer,
            'source_sequence': metadata.sequence.stream,
            'source_timestamp': metadata.timestamp.astimezone(UTC).isoformat(),
            'payload_base64': base64.b64encode(message.data).decode('ascii'),
            'classification': 'PERMANENT',
            'reason': reason,
            'first_failed_at': observed_at,
            'last_failed_at': observed_at,
        }
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8')
        ack = await js.publish(
            config['dead_letter_subject'],
            payload,
            headers={'Nats-Msg-Id': dead_letter_id},
        )
        if ack.stream != config['dead_letter_stream']:
            raise ConfigurationError('DLQ PubAck confirms an unexpected Stream')
        if not getattr(ack, 'duplicate', False):
            self.dlq_total += 1

    async def _write_with_heartbeat(
        self, message: Any, sample: SampleMessage
    ) -> BatchOutcome:
        """Keep native target I/O owned even if the AckWait renewal fails."""
        assert self._adapter is not None
        async def native_write() -> BatchOutcome:
            result: BatchOutcome = await self._adapter.write_batch((sample,))
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
    async def _run(self) -> None:
        assert self._subscription is not None
        config = self._snapshot.service["reliable"]
        backoff = config["retry_base_ms"] / 1000
        max_backoff = config["retry_max_ms"] / 1000
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_capacity_check >= 10:
                self._last_capacity_check = now
                await self._check_capacity()
            try:
                messages = await self._subscription.fetch(
                    batch=1, timeout=1.0
                )
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
                try:
                    sample = SampleMessage.decode(message.data, "reliable")
                    if message.subject != sample.subject("reliable"):
                        raise ValueError("JetStream subject and sample identity mismatch")
                except (ValueError, UnicodeError) as exc:
                    try:
                        await self._dead_letter(message, str(exc))
                    except Exception as failure:
                        self.last_error = f'DLQ_BLOCKED: PubAck unavailable: {failure}'
                        try:
                            await message.nak(delay=min(backoff, max_backoff))
                        except Exception as nak_error:
                            self.last_error = (
                                f'DLQ_BLOCKED: NAK unresolved: {nak_error}'
                            )
                        await self._retry_pause(min(backoff, max_backoff))
                        continue
                    try:
                        await message.ack_sync(timeout=5)
                    except Exception as failure:
                        # The original remains the Broker's responsibility;
                        # the DLQ message is already durably published.
                        self.last_error = f'DLQ transferred; source ACK uncertain: {failure}'
                    continue
                replay_owner = (message.headers or {}).get('Wh-Replay-Delivery', '')
                if replay_owner and replay_owner != self._definition.delivery_id:
                    await message.ack_sync(timeout=5)
                    continue
                if not self._matches(sample):
                    await message.ack_sync(timeout=5)
                    continue
                self.received_total += 1
                if metadata.num_pending > 0:
                    rate = config['replay_max_rate_per_second']
                    now = asyncio.get_running_loop().time()
                    delay = self._last_backlog_output + 1 / rate - now
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._last_backlog_output = asyncio.get_running_loop().time()
                try:
                    assert self._adapter is not None
                    self._active_io = True
                    try:
                        result = await self._write_with_heartbeat(message, sample)
                    finally:
                        self._active_io = False
                    # Target evidence is an execution fact independent of JS ACK.
                    self.write_success_total += result.successful_attempts
                    self.failed_total += result.failed_attempts
                    for latency in result.successful_latencies_ms:
                        timestamp = datetime.now(UTC)
                        if len(self.latency_samples) == self.latency_samples.maxlen:
                            self.latency_overflow_at = timestamp
                        self.latency_samples.append((timestamp, latency))
                    if 0 in result.delivered:
                        self.delivered_total += 1
                    if result.error.startswith('Redis CAS conflicts at samples'):
                        # The target explicitly rejected a same-time conflicting
                        # identity. Repeating it cannot make it a valid version.
                        await self._dead_letter(message, result.error)
                        await message.ack_sync(timeout=5)
                        self._backlog = metadata.num_pending
                        self.last_error = ''
                        backoff = config['retry_base_ms'] / 1000
                        continue
                    if 0 in result.delivered or (not result.error and 0 in result.settled):
                        await message.ack_sync(timeout=5)
                        self._backlog = metadata.num_pending
                        self._last_backlog_progress = time.monotonic()
                        self.last_error = ""
                        backoff = config["retry_base_ms"] / 1000
                        continue
                    raise RuntimeError(result.error or "target result remains unconfirmed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # UNKNOWN is not a definitively failed target I/O attempt.
                    self.retry_total += 1
                    self.last_error = str(exc)
                    # Never ACK on transient/unsettled I/O. JS retains durable ownership.
                    try:
                        await message.nak(delay=min(backoff, max_backoff))
                    except Exception as nak_error:
                        self.last_error = f'{exc}; NAK unresolved: {nak_error}'
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
