"""Independent realtime and durable JetStream consumption."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence

import nats
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.aio.subscription import Subscription

from contracts.sample_message import SampleMessage

logger = logging.getLogger(__name__)
SampleHandler = Callable[[SampleMessage], Awaitable[None]]


class NatsSinkConsumer:
    """Consume a single Sink's own subscription, never a shared queue group."""

    def __init__(
        self,
        servers: Sequence[str],
        subject: str,
        *,
        durable: str | None,
        handler: SampleHandler,
        ack_wait_seconds: float = 30.0,
        max_ack_pending: int = 128,
        stream: str = "WH_HISTORY",
        on_delivery_error: Callable[[str, int], None] | None = None,
    ) -> None:
        if not servers or not subject:
            raise ValueError("NATS servers and subject are required")
        if durable is not None and not durable:
            raise ValueError("durable must not be empty")
        self._servers = list(servers)
        self._subject = subject
        self._durable = durable
        self._handler = handler
        self._ack_wait_seconds = ack_wait_seconds
        self._max_ack_pending = max_ack_pending
        self._stream = stream
        self._on_delivery_error = on_delivery_error
        self._client: Client | None = None
        self._subscription: Subscription | None = None

    async def open(self) -> None:
        """Create a subscription with independent delivery progress."""
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

    async def _on_realtime(self, msg: Msg) -> None:
        try:
            sample = SampleMessage.decode(msg.data, "realtime")
            await self._handler(sample)
        except Exception as exc:
            logger.exception("Realtime message rejected or write failed")
            if self._on_delivery_error is not None:
                self._on_delivery_error(str(exc), 1)

    async def _on_reliable(self, msg: Msg) -> None:
        try:
            sample = SampleMessage.decode(msg.data, "reliable")
            await self._handler(sample)
            await msg.ack()
        except Exception as exc:
            attempts = msg.metadata.num_delivered if msg.metadata is not None else 1
            delay = min(60.0, float(2 ** min(max(attempts - 1, 0), 6)))
            if self._on_delivery_error is not None:
                self._on_delivery_error(str(exc), attempts)
            if attempts >= 10:
                logger.error("JetStream poison message requires operator attention: attempt=%s", attempts)
            logger.exception("Reliable target write failed; requesting redelivery in %ss", delay)
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
                await subscription.drain()
            await client.drain()
        finally:
            await client.close()
