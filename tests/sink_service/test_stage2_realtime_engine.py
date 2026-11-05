"""RealtimeEngine policy, cache and deterministic batching against a fake writer.

The fake writer is an in-process test double for the downstream *port* only; the
engine under test is the production ``RealtimeEngine`` with its real scheduler,
threshold math, authority bookkeeping and 500-point batching.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from contracts.sample_message import SampleMessage
from sink_service.application.realtime import BatchOutcome, RealtimeEngine

pytestmark = [pytest.mark.unit, pytest.mark.component]

FARM = "farm-a"
COLLECTOR = "collector-1"


def _sample(
    sample_id: str,
    *,
    value: float | int | bool | str | None = 1.0,
    device: str = "WT001",
    point: str = "active_power",
    task: str = "task-1",
    when: datetime | None = None,
    staleness: int | None = 5000,
    quality: str = "good",
    farm: str = FARM,
) -> SampleMessage:
    now = datetime.now(UTC)
    return SampleMessage(
        wind_farm_id=farm,
        collector_id=COLLECTOR,
        task_id=task,
        device_id=device,
        point_id=point,
        sample_id=sample_id,
        value=value,
        quality=quality,  # type: ignore[arg-type]
        sample_time=when or now,
        publish_time=now,
        max_staleness_ms=staleness,
    )


class RecordingWriter:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.batches: list[tuple[SampleMessage, ...]] = []
        self.fail_times = fail_times

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        self.batches.append(samples)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("downstream unavailable")
        n = len(samples)
        return BatchOutcome(
            delivered=tuple(range(n)),
            settled=tuple(range(n)),
            successful_attempts=n,
            successful_latencies_ms=tuple(1.0 for _ in samples),
        )


def _engine(writer: RecordingWriter, **overrides: object) -> RealtimeEngine:
    params: dict[str, object] = {
        "writer": writer,
        "wind_farm_id": FARM,
        "collector_id": COLLECTOR,
        "devices": frozenset(),
        "points": frozenset(),
        "policy": "periodic",
        "period_ms": 30,
        "max_points": 1000,
        "max_age_ms": None,
        "absolute_threshold": 1.0,
        "relative_threshold": 0.0,
    }
    params.update(overrides)
    return RealtimeEngine(**params)  # type: ignore[arg-type]


def test_constructor_rejects_bad_policy_interval_and_thresholds() -> None:
    writer = RecordingWriter()
    with pytest.raises(ValueError, match="unknown realtime output policy"):
        _engine(writer, policy="burst")
    with pytest.raises(ValueError, match="invalid realtime interval"):
        _engine(writer, period_ms=0)
    with pytest.raises(ValueError, match="negative change threshold"):
        _engine(writer, absolute_threshold=-0.1)


def test_foreign_source_is_ignored_without_being_counted() -> None:
    engine = _engine(RecordingWriter(), devices=frozenset({"WT001"}), points=frozenset({"p"}))
    assert engine.offer(_sample("a", device="WT999")) is False
    assert engine.offer(_sample("b", point="other")) is False
    assert engine.offer(_sample("c", farm="farm-b")) is False
    assert engine.received_total == 0
    assert engine.dropped_total == 0


def test_stale_sample_is_dropped() -> None:
    engine = _engine(RecordingWriter())
    old = _sample("old", when=datetime.now(UTC) - timedelta(seconds=10), staleness=1000)
    assert engine.offer(old) is False
    assert engine.received_total == 1
    assert engine.dropped_total == 1
    assert engine.source_identity == (FARM, COLLECTOR)


def test_out_of_order_and_duplicate_samples_are_dropped_or_ignored() -> None:
    engine = _engine(RecordingWriter())
    base = datetime.now(UTC)
    first = _sample("a", when=base)
    assert engine.offer(first) is True
    older = _sample("b", when=base - timedelta(milliseconds=50))
    assert engine.offer(older) is False  # older sample_time -> dropped
    same_time = _sample("c", when=base)
    assert engine.offer(same_time) is False  # equal time, different id -> dropped
    assert engine.dropped_total == 2
    duplicate = _sample("a", when=base)
    assert engine.offer(duplicate) is False  # same sample_id -> no-op, not a drop
    assert engine.dropped_total == 2


def test_conflicting_task_authority_quarantines_the_point() -> None:
    engine = _engine(RecordingWriter())
    assert engine.offer(_sample("a", task="task-1")) is True
    intruder = _sample("b", task="task-2")
    assert engine.offer(intruder) is False
    assert "conflicting task authorities" in engine.last_error
    # The quarantined key rejects every later sample, even the original authority.
    assert engine.offer(_sample("c", task="task-1")) is False
    assert engine.received_total == 3
    assert engine.dropped_total == 0


def test_cache_capacity_rejects_a_new_point_but_kept_points_accept_updates() -> None:
    engine = _engine(RecordingWriter(), max_points=1)
    assert (
        engine.offer(_sample("a", point="p1", when=datetime.now(UTC) - timedelta(milliseconds=100)))
        is True
    )
    overflow = _sample("b", point="p2")
    assert engine.offer(overflow) is False
    assert engine.dropped_total == 1
    assert "at capacity" in engine.last_error
    # An existing key still accepts a newer sample.
    update = _sample("a2", point="p1")
    assert engine.offer(update) is True


def test_change_detection_honours_absolute_and_relative_thresholds() -> None:
    engine = _engine(
        RecordingWriter(),
        policy="on_change",
        absolute_threshold=1.0,
        relative_threshold=0.1,
    )
    base = _sample("a", value=10.0)
    # No previous sent value: any first sample is a change.
    assert engine._changed(base, None) is True
    assert engine._changed(_sample("b", value=10.5), base) is False  # below both
    assert engine._changed(_sample("c", value=11.0), base) is True  # absolute hit
    assert engine._changed(_sample("d", value=12.0), base) is True  # relative hit (20%)
    quality = _sample("e", value=10.0, quality="bad")
    assert engine._changed(quality, base) is True  # quality flips are always changes


async def test_periodic_policy_emits_even_without_value_change() -> None:
    writer = RecordingWriter()
    engine = _engine(writer, policy="periodic", period_ms=20)
    await engine.start()
    try:
        engine.offer(_sample("a", value=1.0))
        await asyncio.sleep(0.12)
    finally:
        await engine.stop()
    assert writer.batches, "periodic scheduler must emit on every tick"
    all_ids = [s.sample_id for batch in writer.batches for s in batch]
    assert "a" in all_ids
    assert engine.delivered_total >= 1


async def test_on_change_policy_stays_idle_below_threshold() -> None:
    writer = RecordingWriter()
    engine = _engine(
        writer,
        policy="on_change",
        period_ms=20,
        absolute_threshold=5.0,
        relative_threshold=0.0,
    )
    await engine.start()
    try:
        engine.offer(_sample("a", value=10.0))
        await asyncio.sleep(0.08)
        writes_after_first = len(writer.batches)
        assert writes_after_first >= 1
        # A sub-threshold update must not trigger an additional output.
        engine.offer(_sample("b", value=11.0))
        await asyncio.sleep(0.08)
        assert len(writer.batches) == writes_after_first
        # A change at/above the absolute threshold wakes the scheduler immediately.
        engine.offer(_sample("c", value=20.0))
        await asyncio.sleep(0.08)
        assert len(writer.batches) > writes_after_first
    finally:
        await engine.stop()


async def test_change_with_heartbeat_resends_unchanged_value_on_tick() -> None:
    writer = RecordingWriter()
    engine = _engine(writer, policy="change_with_heartbeat", period_ms=20)
    await engine.start()
    try:
        engine.offer(_sample("a", value=1.0))
        await asyncio.sleep(0.12)
    finally:
        await engine.stop()
    # The single cached point is re-emitted on periodic ticks despite no change.
    emitted = [s for batch in writer.batches for s in batch if s.sample_id == "a"]
    assert len(emitted) >= 2


async def test_batch_is_chunked_at_500_points() -> None:
    writer = RecordingWriter()
    engine = _engine(writer, policy="periodic", period_ms=30, max_points=1000)
    await engine.start()
    try:
        for index in range(650):
            engine.offer(_sample(f"s{index}", point=f"p{index}"))
        await asyncio.sleep(0.1)
    finally:
        await engine.stop()
    # Every physical write is capped at 500 points; the 650 points split 500+150
    # on each periodic tick and every offered point reaches the writer at least once.
    sizes = [len(batch) for batch in writer.batches]
    assert sizes and all(size <= 500 for size in sizes)
    assert {150, 500} <= set(sizes)
    seen = {s.sample_id for batch in writer.batches for s in batch}
    assert len(seen) == 650


async def test_writer_failure_is_retried_and_then_delivered() -> None:
    writer = RecordingWriter(fail_times=2)
    engine = _engine(writer, policy="periodic", period_ms=20)
    await engine.start()
    try:
        engine.offer(_sample("a", value=1.0))
        await asyncio.sleep(0.2)
    finally:
        await engine.stop()
    assert writer.fail_times == 0  # both failures were retried and exhausted
    # After recovery the periodic scheduler delivers (and keeps re-emitting) the point.
    assert engine.delivered_total >= 1
    assert engine.last_error == ""
    assert engine.failed_total == 0  # raised attempts break the cycle, not counted
