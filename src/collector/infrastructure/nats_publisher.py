"""NATS output for one Collector process."""

from __future__ import annotations

import asyncio
import contextlib
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
        self._connect_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def open(self) -> None:
        """Start a reconnect supervisor without blocking collection."""
        self._stopping = False
        if self._connect_task is None:
            self._connect_task = asyncio.create_task(self._connect_loop())

    async def _connect_loop(self) -> None:
        """Retry connections independently of sampled values."""
        while not self._stopping:
            if self._client is not None and not self._client.is_closed:
                await asyncio.sleep(1)
                continue
            try:
                self._client = await nats.connect(
                    servers=self._servers,
                    allow_reconnect=True,
                    max_reconnect_attempts=3,
                    connect_timeout=0.5,
                    reconnect_time_wait=1,
                    pending_size=1024 * 1024,
                )
            except Exception:
                logger.exception("NATS connection failed; retrying")
                await asyncio.sleep(1)

    async def publish(self, message: SampleMessage) -> bool:
        """Confirm Core NATS enqueue or JetStream PubAck without local spooling."""
        client = self._client
        if self._stopping or client is None or not client.is_connected:
            logger.error("NATS unavailable; sample dropped: %s", message.sample_id)
            return False
        try:
            subject = message.subject(self._delivery)
            payload = message.encode()
            if self._delivery == "reliable":
                await asyncio.wait_for(
                    client.jetstream().publish(
                        subject,
                        payload,
                        headers={"Nats-Msg-Id": message.sample_id},
                        timeout=1.0,
                    ),
                    timeout=1.5,
                )
            else:
                await asyncio.wait_for(client.publish(subject, payload), timeout=0.5)
            return True
        except Exception:
            logger.exception("NATS publication not confirmed: sample=%s", message.sample_id)
            return False

    async def close(self) -> None:
        """Stop reconnect attempts, drain pending Core NATS writes and close."""
        self._stopping = True
        task, self._connect_task = self._connect_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.drain(), timeout=3.0)
            except (Exception, asyncio.CancelledError):
                logger.warning("NATS drain could not complete", exc_info=True)
            finally:
                await client.close()
