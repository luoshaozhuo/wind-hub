"""NATS publication adapter; a failure never stalls subsequent acquisition."""
from __future__ import annotations

import logging
from collections.abc import Sequence

import nats

from contracts.sample_message import DeliveryMode, SampleMessage

logger = logging.getLogger(__name__)


class NatsPublisher:
    """One Collector publishes to exactly one configured delivery mode."""

    def __init__(self, servers: Sequence[str], delivery: DeliveryMode) -> None:
        if not servers:
            raise ValueError("NATS servers must not be empty")
        self._servers = list(servers)
        self._delivery = delivery
        self._client: nats.aio.client.Client | None = None

    async def open(self) -> None:
        """Start a reconnecting NATS client without a local disk queue."""
        if self._client is not None:
            return
        try:\n            self._client = await nats.connect(
            servers=self._servers,
            allow_reconnect=True,
            max_reconnect_attempts=-1,\n            connect_timeout=0.5,
            reconnect_time_wait=1,
            pending_size=1024 * 1024,
        )

    async def publish(self, message: SampleMessage) -> bool:
        """Return success only when the appropriate publish boundary is met."""
        client = self._client
        if client is None or not client.is_connected:
            logger.error("NATS unavailable; dropping sample %s", message.sample_id)
            return False
        subject = message.subject(self._delivery)
        try:
            if self._delivery == "reliable":
                await client.jetstream().publish(
                    subject, message.encode(), headers={"Nats-Msg-Id": message.sample_id},
                    timeout=1,
                )
            else:
                await client.publish(subject, message.encode())
        except Exception:
            logger.exception("NATS publish failed: sample_id=%s", message.sample_id)
            return False
        return True

    async def close(self) -> None:
        """Release client resources."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()
