"""Lifecycle and consumption coordination for one independent Sink."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from contracts.sample_message import SampleMessage
from sink_service.application.port import SinkPort
from sink_service.infrastructure.nats_consumer import NatsSinkConsumer

PointKey = tuple[str, str, str]


class SinkState(StrEnum):
    """Sink runtime lifecycle states."""

    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


class SinkRuntime:
    """Own one Sink's connection, subscription and realtime ordering state."""

    def __init__(self, sink: SinkPort, consumer: NatsSinkConsumer, *, realtime: bool) -> None:
        self._sink = sink
        self._consumer = consumer
        self._realtime = realtime
        self._state = SinkState.STOPPED
        self._desired_running = False
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._latest: dict[PointKey, SampleMessage] = {}
        self._last_written: datetime | None = None
        self._failure: str | None = None

    @property
    def state(self) -> SinkState:
        """Current lifecycle state."""
        return self._state

    @property
    def desired_running(self) -> bool:
        """Whether an operator has requested this Sink to run."""
        return self._desired_running

    @property
    def last_written(self) -> datetime | None:
        """Timestamp of the most recent confirmed write."""
        return self._last_written

    @property
    def failure(self) -> str | None:
        """Last observed startup or write failure."""
        return self._failure

    async def start(self) -> None:
        """Open target before subscribing, without reviving a stopped Sink."""
        async with self._lock:
            self._desired_running = True
            if self._state == SinkState.RUNNING:
                return
            try:
                await self._sink.open()
                self._state = SinkState.RUNNING
                await self._consumer.open()
            except Exception as exc:
                self._failure = str(exc)
                self._state = SinkState.FAILED
                await self._consumer.close()
                await self._sink.close()
                raise
            self._failure = None
            self._state = SinkState.RUNNING

    async def stop(self) -> None:
        """Prevent new messages, finish active writes and close the target."""
        async with self._lock:
            self._desired_running = False
            self._state = SinkState.STOPPED
            await self._consumer.close()
            async with self._write_lock:
                await self._sink.close()
            self._state = SinkState.STOPPED

    async def restart(self) -> None:
        """Explicit operator restart."""
        await self.stop()
        await self.start()

    async def write(self, sample: SampleMessage) -> None:
        """Validate realtime order and expiry before confirmed target write."""
        async with self._write_lock:
            if self._state != SinkState.RUNNING:
                raise RuntimeError("Sink is not running")
            if self._realtime:
                if sample.max_staleness_ms is None:
                    raise ValueError("realtime expiry missing")
                if not self._is_fresh(sample):
                    return
                key = self._key(sample)
                previous = self._latest.get(key)
                if previous is not None:
                    if sample.sample_time < previous.sample_time:
                        return
                    if sample.sample_time == previous.sample_time:
                        if sample.sample_id == previous.sample_id:
                            return
                        raise ValueError("ambiguous equal-time realtime samples")
            try:
                await self._sink.write([sample])
            except Exception as exc:
                self._failure = str(exc)
                raise
            if self._realtime:
                self._latest[self._key(sample)] = sample
            self._last_written = datetime.now(UTC)
            self._failure = None

    def latest(self, key: PointKey) -> tuple[SampleMessage | None, bool]:
        """Return cached value and current validity, even without new messages."""
        sample = self._latest.get(key)
        return sample, sample is not None and self._is_fresh(sample)

    def clear_realtime(self) -> None:
        """Invalidate latest-value cache; never delete output target data."""
        if not self._realtime or self._state != SinkState.STOPPED:
            raise RuntimeError("realtime cache can only be cleared while stopped")
        self._latest.clear()

    @staticmethod
    def _key(sample: SampleMessage) -> PointKey:
        return sample.wind_farm_id, sample.device_id, sample.point_id

    @staticmethod
    def _is_fresh(sample: SampleMessage) -> bool:
        if sample.max_staleness_ms is None:
            return False
        now = datetime.now(UTC)
        if sample.sample_time > now:
            return False
        return now - sample.sample_time <= timedelta(milliseconds=sample.max_staleness_ms)
