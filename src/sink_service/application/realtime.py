"""Realtime Delivery policy, bounded latest-value cache and deterministic batches."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from contracts.sample_message import SampleMessage


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """Zero-based per-sample indexes: delivered has confirmed business updates."""
    delivered: tuple[int, ...]
    settled: tuple[int, ...]
    error: str = ""
    successful_attempts: int = 0
    failed_attempts: int = 0
    successful_latencies_ms: tuple[float, ...] = ()


class BatchWriter(Protocol):
    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome: ...


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
        self._authorities: dict[tuple[str, str], str] = {}
        self._conflicted: set[tuple[str, str]] = set()
        self._sent: dict[tuple[str, str], SampleMessage] = {}
        self._sent_at: dict[tuple[str, str], float] = {}
        self._skipped: dict[tuple[str, str], str] = {}
        self._retry_pending = False
        self._wakeup = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.last_error = ""
        self.received_total = 0
        self.dropped_total = 0
        self.delivered_total = 0
        self.failed_total = 0
        self.write_success_total = 0
        self.latency_samples: deque[tuple[datetime, float]] = deque(maxlen=50000)
        self.latency_overflow_at: datetime | None = None

    @property
    def source_identity(self) -> tuple[str, str]:
        """Index subscriptions by immutable wind-farm and Collector identities."""
        return self._wind_farm_id, self._collector_id

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
        return 0 <= age_ms < max_age

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
            if self._absolute == 0 and self._relative == 0:
                return difference > 0
            absolute_hit = self._absolute > 0 and difference >= self._absolute
            relative_hit = (
                self._relative > 0
                and b != 0
                and difference / abs(b) >= self._relative
            )
            return absolute_hit or relative_hit
        return a != b

    def offer(self, sample: SampleMessage) -> bool:
        """Route first, then count received, expired, overwritten and rejected samples."""
        if self._stop.is_set():
            return False
        if (
            sample.wind_farm_id != self._wind_farm_id
            or sample.collector_id != self._collector_id
            or (self._devices and sample.device_id not in self._devices)
            or (self._points and sample.point_id not in self._points)
        ):
            # A sample outside the declared source is not a dropped sample.
            return False
        self.received_total += 1
        if not self._fresh(sample, datetime.now(UTC)):
            self.dropped_total += 1
            return False
        key = self._key(sample)
        authority = self._authorities.get(key)
        if key in self._conflicted:
            return False
        if authority is not None and authority != sample.task_id:
            self._conflicted.add(key)
            self.last_error = (
                f'conflicting task authorities for {sample.device_id}:{sample.point_id}'
            )
            return False
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
        if prior is not None:
            confirmed = self._sent.get(key)
            if confirmed is None or confirmed.sample_id != prior.sample_id:
                self.dropped_total += 1
        self._latest[key] = sample
        self._authorities[key] = sample.task_id
        if self._policy != 'periodic' and self._changed(sample, self._sent.get(key)):
            self._wakeup.set()
        return True

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        task = asyncio.create_task(self._run())
        task.add_done_callback(self._on_task_finished)
        self._task = task

    def _on_task_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            self.last_error = 'Realtime scheduler was cancelled unexpectedly'
            return
        failure = task.exception()
        if failure is not None:
            self.last_error = f'Realtime scheduler terminated: {failure}'
        elif not self._stop.is_set():
            self.last_error = 'Realtime scheduler exited without STOP'

    async def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        task = self._task
        if task is not None:
            if task.cancelled():
                self.last_error = 'Realtime scheduler was cancelled'
            else:
                try:
                    await asyncio.shield(task)
                except Exception as exc:
                    self.last_error = f'Realtime scheduler terminated: {exc}'
            self._task = None
    async def _run(self) -> None:
        """At most one serial output cycle per configured interval."""
        loop = asyncio.get_running_loop()
        next_tick = loop.time() + self._interval
        while not self._stop.is_set():
            delay = max(0.0, next_tick - loop.time())
            if self._policy == 'periodic':
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                event_fired = False
            else:
                try:
                    await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
                except TimeoutError:
                    pass
                event_fired = self._wakeup.is_set()
                self._wakeup.clear()
            if self._stop.is_set():
                return
            now_mono = loop.time()
            periodic_due = now_mono >= next_tick
            if periodic_due:
                next_tick += (
                    max(1, int((now_mono - next_tick) / self._interval) + 1)
                    * self._interval
                )
            now = datetime.now(UTC)
            for key, sample in tuple(self._latest.items()):
                if not self._fresh(sample, now):
                    confirmed = self._sent.get(key)
                    if confirmed is None or confirmed.sample_id != sample.sample_id:
                        self.dropped_total += 1
                    self._latest.pop(key, None)
                    self._authorities.pop(key, None)
                    self._conflicted.discard(key)
                    self._sent.pop(key, None)
                    self._sent_at.pop(key, None)
                    self._skipped.pop(key, None)
            if self._policy == 'periodic' and not periodic_due:
                continue
            if (
                self._policy == 'on_change'
                and not event_fired
                and not (periodic_due and self._retry_pending)
            ):
                continue
            items = [
                sample
                for key, sample in sorted(self._latest.items())
                if (
                    self._skipped.get(key) != sample.sample_id
                    and (
                        (self._policy == 'periodic' and periodic_due)
                        or self._changed(sample, self._sent.get(key))
                        or (
                            self._policy == 'change_with_heartbeat'
                            and periodic_due
                            and now_mono - self._sent_at.get(key, float('-inf'))
                            >= self._interval
                        )
                    )
                )
            ]
            self._retry_pending = False
            for index in range(0, len(items), 500):
                batch = tuple(items[index : index + 500])
                try:
                    outcome = await self._writer.write_batch(batch)
                    if not isinstance(outcome, BatchOutcome):
                        raise RuntimeError("Adapter returned an invalid batch result")
                    delivered = set(outcome.delivered)
                    settled = set(outcome.settled)
                    if (
                        not delivered.issubset(settled)
                        or any(not 0 <= offset < len(batch) for offset in settled)
                        or len(settled) != len(outcome.settled)
                    ):
                        raise RuntimeError("Adapter returned invalid sample indexes")
                except Exception as exc:
                    self.last_error = str(exc)
                    self._retry_pending = True
                    break
                for offset in settled:
                    sample = batch[offset]
                    key = self._key(sample)
                    if offset in delivered:
                        self._sent[key] = sample
                        self._sent_at[key] = loop.time()
                        self._skipped.pop(key, None)
                    else:
                        # A stale/duplicate target-side CAS skip is not a new success.
                        self._skipped[key] = sample.sample_id
                self.delivered_total += len(delivered)
                self.write_success_total += outcome.successful_attempts
                self.failed_total += outcome.failed_attempts
                for value in outcome.successful_latencies_ms:
                    timestamp = datetime.now(UTC)
                    if len(self.latency_samples) == self.latency_samples.maxlen:
                        self.latency_overflow_at = timestamp
                    self.latency_samples.append((timestamp, value))
                self.last_error = outcome.error
                if outcome.error:
                    self._retry_pending = True
                    break
