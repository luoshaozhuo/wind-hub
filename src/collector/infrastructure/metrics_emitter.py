"""Bounded fixed UTC window metrics publication for the Collector process."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

import nats
from nats.aio.client import Client

from collector.application.telemetry import CollectorTelemetry
from contracts.sample_message import validate_subject_token

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FixedWindow:
    end_epoch: int
    subject: str
    payload: bytes


class CollectorMetricsEmitter:
    """Emit versioned UTC-aligned metrics snapshots without blocking PLC reads."""

    def __init__(
        self,
        telemetry: CollectorTelemetry,
        *,
        servers: list[str],
        site_id: str,
        collector_id: str,
        interval_seconds: int,
        max_staleness_seconds: int,
        capacity: int = 16,
    ) -> None:
        if interval_seconds <= 0 or max_staleness_seconds <= 0 or capacity <= 0:
            raise ValueError("metrics publication limits must be positive")
        self._telemetry = telemetry
        self._servers = servers
        self._subject = (
            "wh.metrics."
            + validate_subject_token(site_id)
            + "."
            + validate_subject_token(collector_id)
        )
        self._collector_id = collector_id
        self._site_id = site_id
        self._boot_id = uuid.uuid4().hex
        self._interval = interval_seconds
        self._staleness = max_staleness_seconds
        self._capacity = capacity
        self._pending: deque[FixedWindow] = deque()
        self._task: asyncio.Task[None] | None = None
        self._connection: Client | None = None
        self.published = 0
        self.expired = 0
        self.dropped = 0

    def update_site(self, site_id: str) -> None:
        self._site_id = validate_subject_token(site_id)
        self._subject = (
            "wh.metrics." + self._site_id + "." + validate_subject_token(self._collector_id)
        )

    def _capture(self, end_epoch: int) -> None:
        begin = end_epoch - self._interval
        counts = self._telemetry.fixed_window(begin, end_epoch)
        # Never invent a zero-valued measurement when the interval is empty.
        if not counts:
            return
        document = {
            "schema_version": 1,
            "collector_id": self._collector_id,
            "boot_id": self._boot_id,
            "window_id": f"{self._boot_id}:{begin}:{end_epoch}",
            "window_start": datetime.fromtimestamp(begin, UTC).isoformat(),
            "window_end": datetime.fromtimestamp(end_epoch, UTC).isoformat(),
            "counters": counts,
        }
        payload = json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(self._pending) >= self._capacity:
            self._pending.popleft()
            self.dropped += 1
        self._pending.append(FixedWindow(end_epoch, self._subject, payload))

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="collector-fixed-metrics")

    async def _flush(self) -> None:
        while self._pending:
            item = self._pending[0]
            if time.time() - item.end_epoch > self._staleness:
                self._pending.popleft()
                self.expired += 1
                continue
            try:
                if self._connection is None or not self._connection.is_connected:
                    self._connection = await nats.connect(
                        servers=self._servers,
                        connect_timeout=2,
                        max_reconnect_attempts=2,
                    )
                await self._connection.publish(item.subject, item.payload)
                # Local submission only; this does not prove Sink ingestion.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("fixed window metrics send unconfirmed: %s", exc)
                # An ambiguous send is not retried: downstream may already
                # have received this window.
                self._pending.popleft()
                self.dropped += 1
                continue
            self._pending.popleft()
            self.published += 1

    async def _run(self) -> None:
        next_end = (int(time.time()) // self._interval + 1) * self._interval
        while True:
            await asyncio.sleep(max(0.0, next_end - time.time()))
            now = time.time()
            if now >= next_end:
                self._capture(next_end)
                next_end = (int(now) // self._interval + 1) * self._interval
            await self._flush()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._connection is not None:
            try:
                await asyncio.wait_for(self._connection.close(), timeout=2)
            except TimeoutError:
                logger.error("Collector metrics NATS close exceeded deadline")
            finally:
                self._connection = None
