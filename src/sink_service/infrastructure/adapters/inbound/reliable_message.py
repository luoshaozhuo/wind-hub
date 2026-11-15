"""JetStream message lifecycle adapter for the Reliable Application port."""

from __future__ import annotations

from typing import Any

from sink_service.application.ports.reliable import SourceMetadata


class JetStreamMessage:
    """Expose durable source decisions without leaking NATS methods upstream."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.data: bytes = raw.data
        self.subject: str = raw.subject
        source = raw.metadata
        self.metadata = SourceMetadata(
            num_pending=source.num_pending,
            num_delivered=source.num_delivered,
            timestamp=source.timestamp,
        )

    async def ack(self, timeout: float) -> None:
        await self.raw.ack_sync(timeout=timeout)

    async def retry(self, delay: float) -> None:
        await self.raw.nak(delay=delay)

    async def extend(self) -> None:
        await self.raw.in_progress()
