"""Configuration revision broadcast is best-effort and never rolls back SQLite."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from configuration_service.infrastructure.adapters.outbound.nats.revision_notifier import (
    RevisionBroadcaster,
)


class _Client:
    def __init__(self) -> None:
        self.subject = ""
        self.payload = b""
        self.closed = False

    async def publish(self, subject: str, payload: bytes) -> None:
        self.subject, self.payload = subject, payload

    async def flush(self, *, timeout: float) -> None:
        assert timeout > 0

    async def close(self) -> None:
        self.closed = True


def test_revision_notice_uses_one_json_revision_field(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()

    async def connect(**kwargs: Any) -> _Client:
        assert kwargs["max_reconnect_attempts"] == 0
        return client

    monkeypatch.setattr(
        "configuration_service.infrastructure.adapters.outbound.nats.revision_notifier"
        ".nats.connect",
        connect,
    )
    publisher = RevisionBroadcaster(("nats://127.0.0.1:4222",))
    asyncio.run(publisher._publish_once(12))
    assert client.subject == "wh.config.changed"
    assert client.payload == b'{"revision_id":12}'
    assert client.closed


def test_failed_transport_does_not_crash_broadcast_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[int] = []

    async def fail(revision_id: int) -> None:
        published.append(revision_id)
        raise ConnectionError("NATS unavailable")

    monkeypatch.setattr(RevisionBroadcaster, "_publish_once", fail)
    publisher = RevisionBroadcaster(("nats://127.0.0.1:4222",))
    publisher.start()
    try:
        publisher.enqueue(15)
        publisher._queue.join()
        assert published == [15]
    finally:
        publisher.close()
