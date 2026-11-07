"""Targeted seams for the six Gate-5 performance fixes.

Tests 1-8 are deterministic unit/component tests with no broker:

1. single and multi-Delivery fanout routing
2. source / device / point filtering with negative caching
3. point-route LRU cache FIFO eviction at the 16384 identity capacity floor
4. no stale routes after Delivery removal and hot-update re-registration
5. isolated change keeps the low-latency immediate output path
6. sustained saturation coalesces writes to the configured heartbeat tick
7. recovery returns to immediate triggering after the source goes quiet
8. STOP while a change is pending never writes an extra batch

The Redis shared-clock and 500-point batch contracts live in
``test_stage3_redis_batch_contract.py`` against a real Redis (system mark).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from contracts.sample_batch import BatchSample, SampleBatch
from contracts.sample_message import SampleMessage
from sink_service.application.realtime import BatchOutcome, RealtimeEngine
from sink_service.infrastructure.realtime_runtime import NatsRealtimeIngress

pytestmark = [pytest.mark.component, pytest.mark.performance]

FARM = "farm-a"
COLLECTOR = "collector-1"


def _sample(
    sample_id: str,
    *,
    value: float = 1.0,
    device: str = "WT001",
    point: str = "active_power",
    task: str = "task-1",
    farm: str = FARM,
    collector: str = COLLECTOR,
    staleness: int | None = 5000,
) -> SampleMessage:
    now = datetime.now(UTC)
    return SampleMessage(
        wind_farm_id=farm,
        collector_id=collector,
        task_id=task,
        device_id=device,
        point_id=point,
        sample_id=sample_id,
        value=value,
        quality="good",  # type: ignore[arg-type]
        sample_time=now,
        publish_time=now,
        max_staleness_ms=staleness,
    )


class _Msg:
    def __init__(self, batch: SampleBatch) -> None:
        self.data = batch.encode()
        self.subject = batch.subject()


def _batch_msg(
    sample_id: str,
    *,
    value: float = 1.0,
    device: str = "WT001",
    point: str = "active_power",
    task: str = "task-1",
    farm: str = FARM,
    collector: str = COLLECTOR,
    staleness: int | None = 5000,
) -> _Msg:
    now = datetime.now(UTC)
    batch = SampleBatch(
        batch_id=f"b-{sample_id}",
        mode="realtime",
        wind_farm_id=farm,
        collector_id=collector,
        task_id=task,
        device_id=device,
        publish_time=now,
        samples=(
            BatchSample(
                point_id=point,
                sample_id=sample_id,
                value=value,
                quality="good",
                sample_time=now,
                max_staleness_ms=staleness,
            ),
        ),
    )
    return _Msg(batch)


class FakeEngine:
    """Minimal engine surface exercised by NatsRealtimeIngress."""

    def __init__(
        self,
        delivery_id: str,
        *,
        farm: str = FARM,
        collector: str = COLLECTOR,
        devices: frozenset[str] = frozenset(),
        points: frozenset[str] = frozenset(),
    ) -> None:
        self.delivery_id = delivery_id
        self.source_identity = (farm, collector)
        self._devices = devices
        self._points = points
        self.offers: list[tuple[SampleMessage, datetime | None, bool]] = []

    def accepts(self, device_id: str, point_id: str) -> bool:
        return (not self._devices or device_id in self._devices) and (
            not self._points or point_id in self._points
        )

    def offer(
        self,
        sample: SampleMessage,
        *,
        now: datetime | None = None,
        routed: bool = False,
    ) -> bool:
        self.offers.append((sample, now, routed))
        return True


def _ingress() -> NatsRealtimeIngress:
    # No broker is contacted: the tests drive _on_message directly and only
    # use remove(), whose NATS teardown is skipped while no client exists.
    return NatsRealtimeIngress(
        servers=["nats://127.0.0.1:1"],
        subject_prefix="wh.batch.rt",
    )


class _FakeSubscription:
    """Stands in for the NATS subscription torn down by remove()."""

    def __init__(self) -> None:
        self.unsubscribed = 0

    async def unsubscribe(self) -> None:
        self.unsubscribed += 1


def _register(ingress: NatsRealtimeIngress, engine: FakeEngine) -> None:
    # Mirrors NatsRealtimeIngress() bookkeeping without a broker: the route map
    # and subscription registry must stay aligned so remove() can pop both.
    ingress._engines[engine.delivery_id] = engine
    source = engine.source_identity
    ingress._routes.setdefault(source, {})[engine.delivery_id] = engine
    ingress._subscriptions.setdefault(source, _FakeSubscription())
    # add() invalidates every cached route decision on topology change.
    ingress._target_cache.clear()


# --- Tests 1-4: point-route cache (commits 571f99ea, fee9596e, 41fff0aa) ----


async def test_ingress_routes_single_and_multi_delivery_fanout() -> None:
    ingress = _ingress()
    wide = FakeEngine("d-wide")
    narrow = FakeEngine("d-narrow", devices=frozenset({"WT001"}))
    _register(ingress, wide)
    _register(ingress, narrow)

    sample = _batch_msg("s-1", device="WT001")
    await ingress._on_message(sample)

    assert [engine for engine in (wide, narrow) if engine.offers] == [wide, narrow]
    assert wide.offers[0][0].sample_id == "s-1"
    assert narrow.offers[0][0].sample_id == "s-1"
    # Cache hits carry routed=True and share one freshness clock.
    assert wide.offers[0][1:] == (wide.offers[0][1], True)
    assert narrow.offers[0][1] is wide.offers[0][1]
    assert wide.offers[0][2] is True

    # Second delivery rejects a device it does not own; the wide one still gets it.
    other = _batch_msg("s-2", device="WT002")
    await ingress._on_message(other)
    assert len(wide.offers) == 2
    assert len(narrow.offers) == 1
    cached = ingress._target_cache[(FARM, COLLECTOR, "WT002", "active_power")]
    assert cached == (wide,)

    # A single-Delivery registration still resolves through the same cache.
    solo = _ingress()
    only = FakeEngine("d-only")
    _register(solo, only)
    await solo._on_message(_batch_msg("s-3"))
    assert len(only.offers) == 1
    assert solo._target_cache[(FARM, COLLECTOR, "WT001", "active_power")] == (only,)


async def test_ingress_filters_source_device_and_point_and_negative_caches() -> None:
    ingress = _ingress()
    engine = FakeEngine(
        "d-1",
        devices=frozenset({"WT001"}),
        points=frozenset({"active_power"}),
    )
    _register(ingress, engine)

    # Foreign wind farm / collector has no route entry: the ingress returns
    # before the point cache and must not fabricate a negative record.
    await ingress._on_message(_batch_msg("foreign", farm="farm-b"))
    assert engine.offers == []
    foreign_key = ("farm-b", COLLECTOR, "WT001", "active_power")
    assert foreign_key not in ingress._target_cache

    # Rejected device identity is negative-cached: while the topology is
    # unchanged, repeated arrivals reuse the empty tuple instead of
    # re-evaluating every engine.
    await ingress._on_message(_batch_msg("other-device", device="WT009"))
    assert engine.offers == []
    assert ingress._target_cache[(FARM, COLLECTOR, "WT009", "active_power")] == ()
    await ingress._on_message(_batch_msg("other-device-repeat", device="WT009"))
    assert engine.offers == []

    # A topology change (add) invalidates the whole cache: the new engine
    # owning WT009 picks the identity up on re-resolution.
    late = FakeEngine("d-late", devices=frozenset({"WT009"}))
    _register(ingress, late)
    assert ingress._target_cache == {}
    await ingress._on_message(_batch_msg("other-device-2", device="WT009"))
    assert len(late.offers) == 1
    assert late.offers[0][0].sample_id == "other-device-2"
    # A fresh identity is unaffected by the evicted negative entry.
    await ingress._on_message(_batch_msg("allowed", device="WT001"))
    assert len(engine.offers) == 1
    assert engine.offers[0][0].sample_id == "allowed"

    point_filtered = _batch_msg("bad-point", device="WT001", point="wind_speed")
    await ingress._on_message(point_filtered)
    assert len(engine.offers) == 1
    assert ingress._target_cache[(FARM, COLLECTOR, "WT001", "wind_speed")] == ()


async def test_target_cache_evicts_oldest_identity_at_lru_capacity() -> None:
    ingress = _ingress()
    engine = FakeEngine("d-1")
    _register(ingress, engine)

    limit = max(16384, len(ingress._engines) * 4096)
    assert limit == 16384  # one engine: the floor, not the per-engine 4096

    for index in range(limit):
        await ingress._on_message(_batch_msg(f"cap-{index}", device=f"WT{index:05d}"))
    assert len(ingress._target_cache) == limit
    oldest_key = (FARM, COLLECTOR, "WT00000", "active_power")
    assert ingress._target_cache[oldest_key] == (engine,)

    # The (limit+1)th distinct identity evicts only the oldest FIFO entry;
    # the pre-fix behaviour was a full flush of every cached route.
    overflow_device = f"WT{limit:05d}"
    await ingress._on_message(_batch_msg("cap-overflow", device=overflow_device))
    assert len(ingress._target_cache) == limit
    assert oldest_key not in ingress._target_cache
    overflow_key = (FARM, COLLECTOR, overflow_device, "active_power")
    assert ingress._target_cache[overflow_key] == (engine,)
    assert len(engine.offers) == limit + 1

    # An identity evicted by FIFO pressure is re-resolved on its next arrival.
    await ingress._on_message(_batch_msg("cap-0-repeat", device="WT00000"))
    assert ingress._target_cache[oldest_key] == (engine,)
    assert len(engine.offers) == limit + 2


async def test_cache_has_no_stale_routes_after_remove_and_hot_update() -> None:
    ingress = _ingress()
    wide = FakeEngine("d-wide")
    _register(ingress, wide)
    await ingress._on_message(_batch_msg("before", device="WT009"))
    assert len(wide.offers) == 1
    assert (FARM, COLLECTOR, "WT009", "active_power") in ingress._target_cache

    # Production removal path must invalidate every cached route decision and
    # tear the source subscription down once the last engine leaves.
    await ingress.remove("d-wide")
    assert ingress._target_cache == {}
    assert ingress._routes == {}
    assert ingress._engines == {}
    assert ingress._subscriptions == {}

    # Hot update: a narrower Delivery takes over the same source identity.
    narrow = FakeEngine("d-narrow", devices=frozenset({"WT001"}))
    _register(ingress, narrow)
    await ingress._on_message(_batch_msg("after-reject", device="WT009"))
    assert narrow.offers == []
    await ingress._on_message(_batch_msg("after-accept", device="WT001"))
    assert len(narrow.offers) == 1
    assert narrow.offers[0][0].sample_id == "after-accept"


# --- Tests 5-8: scheduler coalescing (commits 50463da2, 8543acba) -----------


class RecordingWriter:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.batches: list[tuple[SampleMessage, ...]] = []
        self._delay = delay

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        if self._delay:
            await asyncio.sleep(self._delay)
        self.batches.append(samples)
        n = len(samples)
        return BatchOutcome(
            delivered=tuple(range(n)),
            settled=tuple(range(n)),
            successful_attempts=n,
            successful_latencies_ms=tuple(1.0 for _ in samples),
        )


class BlockingWriter:
    """First write blocks until released; records every entered batch."""

    def __init__(self) -> None:
        self.batches: list[tuple[SampleMessage, ...]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def write_batch(self, samples: tuple[SampleMessage, ...]) -> BatchOutcome:
        self.batches.append(samples)
        self.entered.set()
        await self.release.wait()
        n = len(samples)
        return BatchOutcome(
            delivered=tuple(range(n)),
            settled=tuple(range(n)),
            successful_attempts=n,
            successful_latencies_ms=tuple(1.0 for _ in samples),
        )


def _heartbeat_engine(writer: object) -> RealtimeEngine:
    return RealtimeEngine(
        writer=writer,  # type: ignore[arg-type]
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        devices=frozenset(),
        points=frozenset(),
        policy="change_with_heartbeat",
        period_ms=100,
        max_points=1000,
        max_age_ms=None,
        absolute_threshold=0.0,
        relative_threshold=0.0,
    )


async def _wait_for_batches(writer: RecordingWriter, count: int) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while len(writer.batches) < count:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for scheduled output batch")
        await asyncio.sleep(0.002)


async def _wait_until(condition: object, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not bool(condition()):  # type: ignore[operator]
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for engine state")
        await asyncio.sleep(0.005)


async def test_isolated_change_keeps_low_latency_immediate_path() -> None:
    writer = RecordingWriter()
    engine = _heartbeat_engine(writer)
    await engine.start()
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert engine.offer(_sample("isolated-1", value=1.0))
        await _wait_for_batches(writer, 1)
        elapsed = loop.time() - started
        # The immediate path plus the bounded 5 ms microbatch window must stay
        # far below the 100 ms heartbeat tick (regression guard for the
        # pre-fix behaviour of waiting for a tick on every change).
        assert elapsed < 0.05
        assert writer.batches[0][0].sample_id == "isolated-1"
        await asyncio.sleep(0.12)
        assert len(writer.batches) == 1
    finally:
        await engine.stop()


async def test_sustained_saturation_coalesces_to_heartbeat_tick() -> None:
    writer = RecordingWriter(delay=0.02)
    engine = _heartbeat_engine(writer)
    await engine.start()
    try:
        engine.offer(_sample("seed", value=0.0))
        await _wait_for_batches(writer, 1)

        async def produce_changes() -> None:
            for index in range(1, 300):
                engine.offer(_sample(f"burst-{index}", value=float(index)))
                await asyncio.sleep(0.002)

        producer = asyncio.create_task(produce_changes())
        await _wait_until(lambda: engine._saturated)
        batches_when_saturated = len(writer.batches)
        await asyncio.sleep(0.3)
        producer.cancel()
        await asyncio.gather(producer, return_exceptions=True)

        # While saturated the scheduler is bound to the 100 ms tick: at most
        # four cycles fit in the 300 ms observation window plus one trailing
        # cycle. Event-by-event writes would have produced one batch per
        # 2 ms offer (~150 over the same window).
        assert len(writer.batches) - batches_when_saturated <= 5
        # Every coalesced batch is a real 500-point-capable write, not a
        # busy-wait stream of empty wakeups.
        assert all(batch for batch in writer.batches)
    finally:
        await engine.stop()


async def test_recovery_returns_to_immediate_trigger_after_quiet() -> None:
    writer = RecordingWriter(delay=0.02)
    engine = _heartbeat_engine(writer)
    await engine.start()
    try:
        engine.offer(_sample("seed", value=0.0))
        await _wait_for_batches(writer, 1)
        for index in range(1, 100):
            engine.offer(_sample(f"burst-{index}", value=float(index)))
            await asyncio.sleep(0.002)
        await _wait_until(lambda: engine._saturated)

        # Two quiet cycles clear the saturation flag.
        await _wait_until(lambda: not engine._saturated)

        loop = asyncio.get_running_loop()
        before = len(writer.batches)
        started = loop.time()
        engine.offer(_sample("isolated-again", value=999.0))
        await _wait_for_batches(writer, before + 1)
        assert loop.time() - started < 0.05
        assert writer.batches[-1][0].sample_id == "isolated-again"
    finally:
        await engine.stop()


async def test_stop_during_pending_change_writes_nothing_extra() -> None:
    writer = BlockingWriter()
    engine = _heartbeat_engine(writer)
    await engine.start()
    try:
        engine.offer(_sample("in-flight", value=1.0))
        await writer.entered.wait()

        # This change lands while the target I/O is still in flight.
        assert engine.offer(_sample("must-not-write", value=2.0))
        stop = asyncio.create_task(engine.stop())
        await asyncio.sleep(0.02)
        writer.release.set()
        await stop

        written_ids = {sample.sample_id for batch in writer.batches for sample in batch}
        assert written_ids == {"in-flight"}
        assert "must-not-write" not in written_ids
        assert engine._task is None
    finally:
        if engine._task is not None:
            await engine.stop()
