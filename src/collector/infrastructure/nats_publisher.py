"""NATS output for one Collector process."""
from __future__ import annotations

import asyncio
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
                    max_reconnect_attempts=-1,
                    connect_timeout=0.5,
                    reconnect_time_wait=1,
                    pending_size=1024 * 1024,
                )
            except Exception:
                logger.exception("NATS connection failed; retrying")
                await asyncio.sleep(1)


