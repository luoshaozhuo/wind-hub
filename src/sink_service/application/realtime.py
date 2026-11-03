"""Realtime Delivery policy, bounded latest-value cache and deterministic batches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from contracts.sample_message import SampleMessage


class BatchWriter(Protocol):
    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> None: ...


class RealtimeEngine:
    """One serial output owner; callbacks only replace the newest cached sample."""

    def __init__(
        self,
        *,
        writer: BatchWriter,
        wind_farm_id: str,
        collector_id: str,
        devices: frozenset[str],
        points: frozenset[str],
        policy: str,
        period_ms: int,
        max_points: int,
        max_age_ms: int | None,
        absolute_threshold: float,
        relative_threshold: float,
    ) -> None:
        if policy not in {"periodic", "on_change", "change_with_heartbeat"}:
            raise ValueError("unknown realtime output policy")
        if period_ms <= 0 or max_points <= 0:
            raise ValueError("invalid realtime interval or point capacity")
        if not 0 <= absolute_threshold or not 0 <= relative_threshold:
            raise ValueError("negative change threshold")
        self._writer = writer
        self._wind_farm_id = wind_farm_id
        self._collector_id = collector_id
        self._devices = devices
        self._points = points
        self._policy = policy
        self._interval = period_ms / 1000
        self._capacity = max_points
        self._max_age_ms = max_age_ms
        self._absolute = absolute_threshold
        self._relative = relative_threshold
        self._latest: dict[tuple[str, str], SampleMessage] = {}
        self._sent: dict[tuple[str, str], SampleMessage] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_error = ""
        self.dropped_total = 0
        self.delivered_total = 0

    @staticmethod
    def _key(sample: SampleMessage) -> tuple[str, str]:
        return sample.device_id, sample.point_id

    def _fresh(self, sample: SampleMessage, now: datetime) -> bool:
        max_age = sample.max_staleness_ms
        if max_age is None:
            return False
        if self._max_age_ms is not None:
            max_age = min(max_age, self._max_age_ms)
        age_ms = (now - sample.sample_time).total_seconds() * 1000
        return 0 <= age_ms <= max_age

    def _changed(self, current: SampleMessage, previous: SampleMessage | None) -> bool:
        if previous is None or current.quality != previous.quality:
            return True
        a, b = current.value, previous.value
        if a is None or b is None:
            return a != b
        if isinstance(a, bool) or isinstance(b, bool):
            return a != b
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            difference = abs(a - b)
            scale = max(abs(b), abs(a))
            return difference > self._absolute and (
                scale == 0 or difference > self._relative * scale
            )
        return a != b

    def offer(self, sample: SampleMessage) -> bool:
        """Called in the NATS subscription callback; never blocks on target I/O."""
        if (
            self._stop.is_set()
            or sample.wind_farm_id != self._wind_farm_id
            or sample.collector_id != self._collector_id
            or (self._devices and sample.device_id not in self._devices)
            or (self._points and sample.point_id not in self._points)
            or not self._fresh(sample, datetime.now(UTC))
        ):
            self.dropped_total += 1
            return False
        key = self._key(sample)
        prior = self._latest.get(key)
        if prior is not None and (
            sample.sample_time < prior.sample_time
            or (
                sample.sample_time == prior.sample_time
                and sample.sample_id != prior.sample_id
            )
        ):
            self.dropped_total += 1
            return False
        if prior is not None and sample.sample_id == prior.sample_id:
            return False
        if prior is None and len(self._latest) >= self._capacity:
            self.dropped_total += 1
            self.last_error = "realtime latest-value cache at capacity"
            return False
        self._latest[key] = sample
        return True

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            await task

    async def _run(self) -> None:
        """At most one serial output cycle per configured interval."""
        loop = asyncio.get_running_loop()
        next_tick = loop.time() + self._interval
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(0.0, next_tick - loop.time())
                )
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            now_mono = loop.time()
            next_tick += (
                max(1, int((now_mono - next_tick) / self._interval) + 1)
                * self._interval
            )
            now = datetime.now(UTC)
            for key, sample in tuple(self._latest.items()):
                if not self._fresh(sample, now):
                    self._latest.pop(key, None)
                    self._sent.pop(key, None)
                    self.dropped_total += 1
            periodic = self._policy in {"periodic", "change_with_heartbeat"}
            items = [
                sample
                for key, sample in sorted(self._latest.items())
                if periodic or self._changed(sample, self._sent.get(key))
            ]
            for index in range(0, len(items), 500):
                batch = tuple(items[index : index + 500])
                try:
                    await self._writer.write_batch(batch)
                except Exception as exc:
                    self.last_error = str(exc)
                    # Next fixed-rate tick retries the last unconfirmed values.
                    break
                for sample in batch:
                    self._sent[self._key(sample)] = sample
                self.delivered_total += len(batch)
                self.last_error = ""
