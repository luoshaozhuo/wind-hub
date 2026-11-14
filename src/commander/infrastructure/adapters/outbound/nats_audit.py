"""NATS JetStream connection owned by the Commander audit process."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from commander.infrastructure.adapters.outbound.audit_publisher import (
    AuditPublisher,
    JetStreamClient,
)


class NatsAuditTransport:
    """Connect lazily to NATS; publish ACKs are required before outbox removal."""

    def __init__(
        self,
        servers: Sequence[str],
        *,
        stream: str,
        subject: str,
        publish_timeout: float = 5.0,
    ) -> None:
        if not servers or any(not server.strip() for server in servers):
            raise ValueError("at least one NATS server is required")
        if not stream.strip() or not subject.strip() or publish_timeout <= 0:
            raise ValueError("invalid audit destination")
        self._servers = tuple(servers)
        self._stream = stream
        self._subject = subject
        self._timeout = publish_timeout
        self._connection: Any = None
        self._publisher: AuditPublisher | None = None

    async def connect(self) -> None:
        if self._connection is not None:
            return
        import nats

        connection = await nats.connect(servers=list(self._servers))
        self._connection = connection
        # nats-py's JetStreamContext carries extra optional publish kwargs;
        # the boundary contract is AuditPublisher's own JetStreamClient protocol.
        client = cast(JetStreamClient, connection.jetstream(timeout=self._timeout))
        self._publisher = AuditPublisher(client, self._subject, self._stream)

    async def publish(self, event: Mapping[str, Any]) -> None:
        if self._publisher is None:
            await self.connect()
        assert self._publisher is not None
        await self._publisher.publish(event)

    async def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        await connection.drain()
        self._connection = None
        self._publisher = None
