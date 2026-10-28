"""NATS output for one Collector process."""
from __future__ import annotations

import logging
from collections.abc import Sequence

import nats
from nats.aio.client import Client

from contracts.sample_message import DeliveryMode, SampleMessage

logger = logging.getLogger(__name__)


class NatsPublisher:
    """Publish samples without disk buffering or blocking acquisition on reconnect."""

    def __init__(self, servers: Sequence[str], delivery: DeliveryMode) -> None:
        if not servers:
            raise ValueError("NATS servers must not be empty")
        self._servers = list(servers)
        self._delivery = delivery
        self._client: Client | None = None

    async def open(self) -> None:
        """Try connecting; a failed connection is reported but not fatal."""
        if self._client is not None:
            return
        try:
            self._client = await nats.connect(
                servers=self._servers,
                allow_reconnect=True,
                max_reconnect_attempts=-1,
                connect_timeout=0.5,
                reconnect_time_wait=1,
                pending_size=1024 * 1024,
            )
        except Exception:
            logger.exception("NATS connection failed; collector will continue sampling")

    async def publish(self, message: SampleMessage) -> bool:
        """Publish once, reporting failure rather than retaining an unbounded queue."""
        client = self._client
        if client is None:
            await self.open()
            client = self._client
        if client is None or not client.is_connected:
            logger.error("NATS unavailable; dropped sample_id=%s", message.sample_id)
            return False
        try:
            if self._delivery == "reliable":
                await client.jetstream().publish(
                    message.subject(self._delivery),
                    message.encode(),
                    headers={"Nats-Msg-Id": message.sample_id},
                    timeout=1.0,
                )
            else:
                await client.publish(message.subject(self._delivery), message.encode())
        except Exception:
            logger.exception("NATS publish failed: sample_id=%s", message.sample_id)
            return False
        return True

    async def close(self) -> None:
        """Close the client without retaining stale connection state."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()
