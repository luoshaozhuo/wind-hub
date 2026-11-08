"""Independent realtime and durable consumption of version-2 batches."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from uuid import uuid4

import nats
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from contracts.sample_batch import (
    DEFAULT_MAX_BATCH_BYTES,
    DEFAULT_MAX_SAMPLES_PER_BATCH,
    SampleBatch,
    validate_subject_token,
)

logger = logging.getLogger(__name__)
BatchHandler = Callable[[SampleBatch], Awaitable[None]]


class NatsSinkConsumer:
    """Consume each source batch once per Sink without sharing a queue group."""

    def __init__(
        self,
        servers: Sequence[str],
        subject: str,
        *,
        durable: str | None,
        handler: BatchHandler,
        ack_wait_seconds: float = 30.0,
        max_ack_pending: int = 128,
        stream: str = "WH_HISTORY",
        max_samples_per_batch: int = DEFAULT_MAX_SAMPLES_PER_BATCH,
        max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
        on_delivery_error: Callable[[str, int], None] | None = None,
    ) -> None:
        if not servers or not subject:
            raise ValueError("NATS servers and subject are required")
        if not subject.startswith(("wh.rt.batch.", "wh.hist.batch.")):
            raise ValueError("batch subscription subject required")
        if durable is not None:
            validate_subject_token(durable)
        if ack_wait_seconds <= 0 or max_ack_pending <= 0:
            raise ValueError("invalid consumer limits")
        if max_samples_per_batch <= 0 or max_batch_bytes <= 0:
            raise ValueError("invalid batch limits")
        self._servers = list(servers)
        self._subject = subject
        self._durable = durable
        self._handler = handler
        self._ack_wait_seconds = ack_wait_seconds
        self._max_ack_pending = max_ack_pending
        self._stream = stream
        self._max_samples = max_samples_per_batch
        self._max_bytes = max_batch_bytes
        self._on_delivery_error = on_delivery_error
        self._client: Client | None = None
        self._subscription: Subscription | None = None

    async def open(self) -> None:
        """Create the dedicated realtime subscription or durable consumer."""
        if self._client is not None:
            return
        client = await nats.connect(
            servers=self._servers,
            allow_reconnect=True,
            max_reconnect_attempts=3,
            connect_timeout=0.5,
            reconnect_time_wait=1,
        )
        try:
            if self._durable is None:
                subscription = await client.subscribe(
                    self._subject,
                    cb=self._on_realtime,
                    pending_msgs_limit=1024,
                    pending_bytes_limit=4 * 1024 * 1024,
                )
            else:
                from nats.js.api import AckPolicy, ConsumerConfig

                subscription = await client.jetstream().subscribe(
                    self._subject,
                    durable=self._durable,
                    stream=self._stream,
                    manual_ack=True,
                    config=ConsumerConfig(
                        durable_name=self._durable,
                        filter_subject=self._subject,
                        ack_policy=AckPolicy.EXPLICIT,
                        ack_wait=self._ack_wait_seconds,
                        max_ack_pending=self._max_ack_pending,
                    ),
                    cb=self._on_reliable,
                )
        except BaseException:
            await client.close()
            raise
        self._client = client
        self._subscription = subscription

    def _decode(self, msg: Msg, mode: str) -> SampleBatch:
        """Decode and check the actual source identity and channel."""
        return SampleBatch.decode(
            msg.data,
            "realtime" if mode == "realtime" else "reliable",
            subject=msg.subject,
            max_samples=self._max_samples,
            max_bytes=self._max_bytes,
        )

    async def _on_realtime(self, msg: Msg) -> None:
        try:
            batch = self._decode(msg, "realtime")
            await self._handler(batch)
        except Exception as exc:
            logger.exception("Realtime batch rejected or write failed")
            if self._on_delivery_error is not None:
                self._on_delivery_error(str(exc), 1)

    async def _to_dlq(self, msg: Msg, error: Exception) -> None:
        """Persist the entire undecodable source before acknowledging it."""
        if self._client is None or self._durable is None:
            raise RuntimeError("DLQ requires an active durable consumer")
        metadata = msg.metadata
        envelope = {
            "source_subject": msg.subject,
            "source_stream": self._stream,
            "source_consumer": self._durable,
            "source_sequence": metadata.sequence.stream if metadata is not None else None,
            "reason": str(error),
            "payload_base64": base64.b64encode(msg.data).decode("ascii"),
        }
        await self._client.jetstream().publish(
            f"wh.dlq.batch.{self._durable}",
            json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            timeout=2,
        )

    async def _on_reliable(self, msg: Msg) -> None:
        attempts = msg.metadata.num_delivered if msg.metadata is not None else 1
        try:
            batch = self._decode(msg, "reliable")
        except Exception as exc:
            # Permanently malformed or incompatible messages have no safe
            # per-point interpretation; the raw payload must survive.
            try:
                await self._to_dlq(msg, exc)
                await msg.ack()
            except Exception as dlq_error:
                logger.exception("DLQ unavailable; preserving source ACK responsibility")
                if self._on_delivery_error is not None:
                    self._on_delivery_error(str(dlq_error), attempts)
                await msg.nak(delay=30)
            return
        try:
            await self._handler(batch)
            await msg.ack()
        except Exception as exc:
            delay = min(60.0, float(2 ** min(max(attempts - 1, 0), 6)))
            if self._on_delivery_error is not None:
                self._on_delivery_error(str(exc), attempts)
            logger.exception("Reliable batch not confirmed; retry in %ss", delay)
            await msg.nak(delay=delay)

    async def clear(self) -> int:
        """Skip only this Sink's durable backlog, without purging shared messages."""
        if self._client is not None:
            raise RuntimeError("stop Sink before clearing its durable consumer")
        if self._durable is None:
            raise RuntimeError("realtime subscribers do not have JetStream backlog")
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        client = await nats.connect(servers=self._servers, connect_timeout=2.0)
        try:
            manager = client.jetstream()
            info = await manager.consumer_info(self._stream, self._durable)
            skipped = int(info.num_pending + info.num_ack_pending)
            if not await manager.delete_consumer(self._stream, self._durable):
                raise RuntimeError("durable consumer deletion was rejected")
            await manager.add_consumer(
                self._stream,
                config=ConsumerConfig(
                    durable_name=self._durable,
                    filter_subject=self._subject,
                    deliver_policy=DeliverPolicy.NEW,
                    deliver_subject=f"_INBOX.{uuid4().hex}",
                    ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=self._ack_wait_seconds,
                    max_ack_pending=self._max_ack_pending,
                ),
            )
            return skipped
        finally:
            await client.close()

    async def pending(self) -> int | None:
        """Return own durable backlog when JetStream is reachable."""
        if self._durable is None or self._client is None or not self._client.is_connected:
            return None
        try:
            info = await self._client.jetstream().consumer_info(self._stream, self._durable)
        except Exception:
            logger.warning("JetStream backlog query failed", exc_info=True)
            return None
        return int(info.num_pending + info.num_ack_pending)

    @property
    def connected(self) -> bool:
        """Whether the existing subscription's NATS transport is connected."""
        return self._client is not None and self._client.is_connected

    async def close(self) -> None:
        """Drain pending handlers before releasing the NATS subscription."""
        subscription, self._subscription = self._subscription, None
        client, self._client = self._client, None
        if client is None:
            return
        try:
            if subscription is not None and self._durable is None:
                await subscription.drain()  # type: ignore[no-untyped-call]
            await client.drain()
        finally:
            await client.close()
