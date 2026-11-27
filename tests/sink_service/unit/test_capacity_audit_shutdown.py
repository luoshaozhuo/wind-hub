"""Capacity audit shutdown distinguishes internal fetch cancellation from caller cancellation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.sink_service.system.test_capacity_r14_sustained import _stop_audit_consumer


class _Connection:
    def __init__(self) -> None:
        self.is_closed = False
        self._reader: object | None = object()
        self._writer: object | None = object()
        self._closed_fut = SimpleNamespace(done=lambda: self.is_closed)


class _Client:
    def __init__(self) -> None:
        self._sync_task: object | None = object()
        self._conns = {1: _Connection()}

    async def close(self) -> None:
        self._sync_task = None
        for connection in self._conns.values():
            connection.is_closed = True
            connection._reader = None
            connection._writer = None


class _Consumer:
    def __init__(self, pending: set[asyncio.Task[None]]) -> None:
        self._fetcher = SimpleNamespace(_pending_tasks=pending)
        self._client = _Client()

    async def stop(self) -> None:
        raise asyncio.CancelledError


async def test_internal_fetch_cancellation_recovers_and_closes_client() -> None:
    async def pending_fetch() -> None:
        await asyncio.Event().wait()

    fetch = asyncio.create_task(pending_fetch())
    consumer = _Consumer({fetch})
    error, closed = await _stop_audit_consumer(cast(Any, consumer))
    assert error == "aiokafka_internal_fetch_cancelled"
    assert closed is True
    assert fetch.done()
    assert consumer._client._sync_task is None
    assert all(connection._closed_fut.done() for connection in consumer._client._conns.values())


async def test_caller_cancellation_is_not_rewritten_as_recovery() -> None:
    consumer = _Consumer(set())
    running = asyncio.create_task(_stop_audit_consumer(cast(Any, consumer)))
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
