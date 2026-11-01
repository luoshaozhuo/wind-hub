"""Reliable ordered outbox delivery with retry and bounded shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from concurrent.futures import Executor
from functools import partial
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class AuditJournal(Protocol):
    def pending_events(self, limit: int = 100) -> tuple[dict[str, Any], ...]: ...
    def mark_published(self, event_id: str) -> None: ...
    def record_publish_failure(self, event_id: str) -> None: ...


class AuditTransport(Protocol):
    async def publish(self, event: Mapping[str, Any]) -> None: ...

    async def close(self) -> None: ...


class AuditDelivery:
    """At-least-once publication. Never acknowledge SQLite before JetStream."""

    def __init__(
        self,
        journal: AuditJournal,
        transport: AuditTransport,
        writer: Executor,
        *,
        retry_base: float = 0.2,
        retry_max: float = 10.0,
    ) -> None:
        if retry_base <= 0 or retry_max < retry_base:
            raise ValueError("invalid retry intervals")
        self._journal = journal
        self._transport = transport
        self._writer = writer
        self._retry_base = retry_base
        self._retry_max = retry_max
        self._stop = asyncio.Event()
        self._stop_deadline: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._published = 0
        self._failures = 0

    @property
    def published(self) -> int:
        return self._published

    @property
    def failures(self) -> int:
        return self._failures

    @property
    def operational(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("audit delivery already started")
        self._stop.clear()
        self._stop_deadline = None
        self._task = asyncio.create_task(self._run(), name="commander:audit")

    async def stop(self, *, timeout: float = 5.0) -> bool:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._stop_deadline = time.monotonic() + timeout
        self._stop.set()
        task = self._task
        if task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            # A publisher still in I/O must not be cancelled and forgotten.
            return False
        except Exception:
            logger.exception("Commander audit worker failed")
            self._task = None
            return False
        self._task = None
        return True

    async def _run(self) -> None:
        consecutive_failures = 0
        while True:
            if self._stop_deadline is not None and time.monotonic() >= self._stop_deadline:
                raise TimeoutError("audit drain deadline exceeded")
            loop = asyncio.get_running_loop()
            pending = await loop.run_in_executor(
                self._writer, partial(self._journal.pending_events, limit=100)
            )
            if not pending:
                if self._stop.is_set():
                    return
                await self._sleep_or_stop(0.1)
                continue
            for event in pending:
                try:
                    await self._transport.publish(event)
                    await loop.run_in_executor(
                        self._writer, partial(self._journal.mark_published, event["event_id"])
                    )
                except Exception:
                    self._failures += 1
                    consecutive_failures += 1
                    await loop.run_in_executor(
                        self._writer,
                        partial(self._journal.record_publish_failure, event["event_id"]),
                    )
                    logger.exception(
                        "audit publication failed",
                        extra={"event_id": event["event_id"]},
                    )
                    backoff = min(
                        self._retry_max,
                        self._retry_base * 2 ** min(consecutive_failures - 1, 12),
                    )
                    await self._sleep_or_stop(backoff)
                    break
                else:
                    self._published += 1
                    consecutive_failures = 0

    async def _sleep_or_stop(self, seconds: float) -> None:
        deadline = self._stop_deadline
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("audit drain deadline exceeded")
            await asyncio.sleep(min(seconds, remaining))
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
