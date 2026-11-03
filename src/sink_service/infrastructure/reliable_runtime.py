"""JetStream reliable Delivery: durable processing, bounded retries and DLQ ownership."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import nats
import yaml
from contracts.sample_message import SampleMessage
from nats.aio.client import Client
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig

from sink_service.application.config import template_path
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot, config_sources
from sink_service.infrastructure.realtime_adapters import KafkaOutput, RedisOutput

logger = logging.getLogger(__name__)


def _template(snapshot: ValidatedSnapshot, family: str, identifier: str, version: str) -> dict[str, Any]:
    _, root = config_sources(snapshot.service)
    path = template_path(root, family, identifier, version)
    for file in snapshot.files:
        if file.path == path:
            value = yaml.safe_load(file.content)
            if not isinstance(value, dict):
                raise ConfigurationError(f"invalid reliable template: {path}")
            return value
    raise ConfigurationError(f"missing reliable template: {path}")


class ReliableDeliveryPort:
    """One durable JetStream Consumer and one in-flight target write per Delivery."""

    def __init__(
        self,
        definition: DeliveryDefinition,
        snapshot: ValidatedSnapshot,
    ) -> None:
        self._definition = definition
        self._snapshot = snapshot
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._client: Client | None = None
        self._subscription: Any = None
        self._adapter: Any = None
        self.last_error = ""
        self.delivered_total = 0
        self.failed_total = 0
        self.retry_total = 0
        self.dlq_total = 0
        self._started_at = datetime.now(UTC)

    @property
    def metrics(self) -> dict[str, float]:
        return {
            "delivered_total": float(self.delivered_total),
            "failed_total": float(self.failed_total),
            "retry_total": float(self.retry_total),
            "dlq_total": float(self.dlq_total),
        }

    def _create_adapter(self) -> Any:
        sink = self._snapshot.sinks[self._definition.sink_id]
        template = _template(
            self._snapshot, "sinks", sink["template_id"], sink["template_version"]
        )
        caps = template["capabilities"]
        if not caps["reliable"]:
            raise ConfigurationError("Sink does not support reliable delivery")
        connection = sink["connection"]
        output = template["output"]
        if template["kind"] == "kafka":
            if output["business_duplicate_policy"] not in ("allow_at_least_once", "require_dedup"):
                raise ConfigurationError("Kafka reliable target requires a duplicate policy")
            if output["business_duplicate_policy"] == "require_dedup":
                # No independently verified downstream business deduplicator is configured.
                raise ConfigurationError("require_dedup has no verified downstream owner")
            return KafkaOutput(
                servers=list(connection["bootstrap_servers"]),
                topic=output["topic"],
                timeout_ms=output["write_timeout_ms"],
                credential_ref=connection.get("credential_ref", ""),
            )
        if template["kind"] == "redis" and caps["reliable"] and caps["idempotent"]:
            return RedisOutput(
                host=connection["host"],
                port=connection["port"],
                namespace=output["namespace"],
                timeout_ms=output["write_timeout_ms"],
                credential_ref=connection.get("credential_ref", ""),
            )
        raise ConfigurationError("Reliable Sink kind or idempotence policy unsupported")

    @staticmethod
    def _durable_id(snapshot: ValidatedSnapshot, delivery_id: str) -> str:
        seed = f"{snapshot.service['deployment_id']}:{delivery_id}".encode("utf-8")
        return "WH_SINK_" + hashlib.sha256(seed).hexdigest()[:24]

    async def start(self) -> None:
        if self._task is not None:
            return
        adapter = self._create_adapter()
        await adapter.open()
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
            self._stop.clear()
            self._task = asyncio.create_task(self._run())
        except BaseException:
            if self._client is not None:
                await self._client.close()
                self._client = None
            await adapter.close()
            self._adapter = None
            raise

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            await task
            self._task = None
        client = self._client
        if client is not None:
            await client.close()
            self._client = None
        adapter = self._adapter
        if adapter is not None:
            await adapter.close()
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
        record = {
            'delivery_id': self._definition.delivery_id,
            'subject': message.subject,
            'source_stream': metadata.stream,
            'consumer': metadata.consumer,
            'source_sequence': metadata.sequence.stream,
            'source_timestamp': metadata.timestamp.astimezone(UTC).isoformat(),
            'payload_base64': base64.b64encode(message.data).decode('ascii'),
            'classification': 'PERMANENT',
            'reason': reason,
            'first_failed_at': datetime.now(UTC).isoformat(),
            'last_failed_at': datetime.now(UTC).isoformat(),
        }
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8')
        identity = (
            f'{self._definition.delivery_id}:{metadata.stream}:'
            f'{metadata.sequence.stream}'
        ).encode('utf-8')
        digest = hashlib.sha256(identity).hexdigest()
        await js.publish(
            config["dead_letter_subject"],
            payload,
            headers={"Nats-Msg-Id": digest},
        )
        self.dlq_total += 1

    async def _write_with_heartbeat(
        self, message: Any, sample: SampleMessage
    ) -> BatchOutcome:
        """Keep native target I/O owned even if the AckWait renewal fails."""
        assert self._adapter is not None
        task = asyncio.create_task(self._adapter.write_batch((sample,)))
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
            raise RuntimeError('JetStream ACK deadline extension failed') from progress_error
        return result

    async def _run(self) -> None:
        assert self._subscription is not None
        config = self._snapshot.service["reliable"]
        backoff = config["retry_base_ms"] / 1000
        max_backoff = config["retry_max_ms"] / 1000
        while not self._stop.is_set():
            try:
                messages = await self._subscription.fetch(
                    batch=1, timeout=1.0
                )
            except (TimeoutError, NatsTimeoutError):
                continue
            except Exception as exc:
                self.last_error = str(exc)
                self.retry_total += 1
                await asyncio.sleep(min(backoff, max_backoff))
                backoff = min(backoff * 2, max_backoff)
                continue
            for message in messages:
                if self._stop.is_set():
                    return
                try:
                    sample = SampleMessage.decode(message.data, "reliable")
                    if message.subject != sample.subject("reliable"):
                        raise ValueError("JetStream subject and sample identity mismatch")
                except (ValueError, UnicodeError) as exc:
                    try:
                        await self._dead_letter(message, str(exc))
                        await message.ack_sync(timeout=5)
                    except Exception as inner:
                        self.last_error = f"DLQ publication not confirmed: {inner}"
                        await message.nak(delay=backoff)
                    continue
                replay_owner = (message.headers or {}).get('Wh-Replay-Delivery', '')
                if replay_owner and replay_owner != self._definition.delivery_id:
                    await message.ack_sync(timeout=5)
                    continue
                if not self._matches(sample):
                    await message.ack_sync(timeout=5)
                    continue
                try:
                    assert self._adapter is not None
                    result = await self._write_with_heartbeat(message, sample)
                    if 0 in result.delivered or (not result.error and 0 in result.settled):
                        await message.ack_sync(timeout=5)
                        self.delivered_total += int(0 in result.delivered)
                        self.last_error = ""
                        backoff = config["retry_base_ms"] / 1000
                        continue
                    raise RuntimeError(result.error or "target result remains unconfirmed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.failed_total += 1
                    self.retry_total += 1
                    self.last_error = str(exc)
                    # Never ACK on transient/unsettled I/O. JS retains durable ownership.
                    await message.nak(delay=min(backoff, max_backoff))
                    backoff = min(backoff * 2, max_backoff)


class ReliableFactory:
    """Construct process-local Reliable ports, with stable Durable identity."""

    def __call__(
        self, definition: DeliveryDefinition, snapshot: ValidatedSnapshot
    ) -> ReliableDeliveryPort:
        if definition.channel != "reliable":
            raise ConfigurationError("ReliableFactory cannot own realtime Delivery")
        return ReliableDeliveryPort(definition, snapshot)
