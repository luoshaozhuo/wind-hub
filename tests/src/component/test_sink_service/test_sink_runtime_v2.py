"""SinkRuntime.write_batch 的实时去重/权威校验与可靠直通组件测试。

Fake SinkPort/ConsumerPort 隔离输出目标与 NATS；所有过滤、冲突裁决与状态
守卫均走真实 SinkRuntime 生产代码。真实重投与 DLQ 语义见 consumer 组件与
integration 层测试。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.sample_batch import DeliveryMode, SampleBatch, SampleItem
from sink_service.application.point_value import SinkPointValue
from sink_service.application.runtime import SinkRuntime, SinkState

pytestmark = [pytest.mark.component, pytest.mark.mock_service]


class FakeSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.writes: list[list[SinkPointValue]] = []
        self.opened = False
        self.closed = False
        self._fail = fail

    async def open(self) -> None:
        self.opened = True

    async def write(self, values: list[SinkPointValue]) -> None:
        if self._fail:
            raise RuntimeError("target unavailable")
        self.writes.append(values)

    async def close(self) -> None:
        self.closed = True


class FakeConsumer:
    def __init__(self) -> None:
        self.connected = True
        self.opened = False
        self.closed = False
        self.cleared = 0
        self.backlog: int | None = 7

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def clear(self) -> int:
        self.cleared += 1
        return 3

    async def pending(self) -> int | None:
        return self.backlog


def _item(
    point: str,
    sample_id: str,
    *,
    sample_time: datetime,
    max_staleness_ms: int | None,
    value: float | int | bool | str | None = 1,
) -> SampleItem:
    return SampleItem(
        point_id=point,
        sample_id=sample_id,
        value=value,
        quality="good",
        sample_time=sample_time,
        max_staleness_ms=max_staleness_ms,
    )


def _batch(
    *,
    mode: DeliveryMode,
    samples: tuple[SampleItem, ...],
    collector_id: str = "collector-1",
    task_id: str = "wtg-01",
    batch_id: str = "b-1",
) -> SampleBatch:
    return SampleBatch(
        batch_id=batch_id,
        mode=mode,
        wind_farm_id="farm-a",
        collector_id=collector_id,
        task_id=task_id,
        device_id="wtg-01",
        publish_time=datetime.now(UTC),
        samples=samples,
    )


async def _runtime(*, realtime: bool, sink: FakeSink | None = None) -> tuple[SinkRuntime, FakeSink]:
    sink = sink or FakeSink()
    runtime = SinkRuntime(sink, FakeConsumer(), realtime=realtime)
    await runtime.start()
    assert runtime.state is SinkState.RUNNING
    return runtime, sink


async def test_write_batch_requires_running_state() -> None:
    runtime = SinkRuntime(FakeSink(), FakeConsumer(), realtime=False)
    batch = _batch(
        mode="reliable",
        samples=(_item("p1", "s-1", sample_time=datetime.now(UTC), max_staleness_ms=None),),
    )
    with pytest.raises(RuntimeError, match="not running"):
        await runtime.write_batch(batch)


async def test_reliable_batch_delivers_every_sample_in_one_write() -> None:
    runtime, sink = await _runtime(realtime=False)
    now = datetime.now(UTC)
    batch = _batch(
        mode="reliable",
        samples=(
            _item("p1", "s-1", sample_time=now, max_staleness_ms=None),
            _item("p2", "s-2", sample_time=now, max_staleness_ms=None, value="x"),
        ),
    )
    await runtime.write_batch(batch)
    assert len(sink.writes) == 1
    assert {value.point_id for value in sink.writes[0]} == {"p1", "p2"}
    assert runtime.last_written is not None
    assert runtime.failure is None


async def test_realtime_stale_and_future_samples_are_skipped() -> None:
    runtime, sink = await _runtime(realtime=True)
    now = datetime.now(UTC)
    batch = _batch(
        mode="realtime",
        samples=(
            _item(
                "fresh",
                "s-1",
                sample_time=now - timedelta(milliseconds=100),
                max_staleness_ms=5000,
            ),
            _item("stale", "s-2", sample_time=now - timedelta(seconds=60), max_staleness_ms=5000),
            _item("future", "s-3", sample_time=now + timedelta(minutes=5), max_staleness_ms=5000),
        ),
    )
    await runtime.write_batch(batch)
    assert len(sink.writes) == 1
    assert [value.point_id for value in sink.writes[0]] == ["fresh"]


async def test_realtime_all_stale_batch_writes_nothing() -> None:
    runtime, sink = await _runtime(realtime=True)
    now = datetime.now(UTC)
    batch = _batch(
        mode="realtime",
        samples=(
            _item(
                "stale",
                "s-1",
                sample_time=now - timedelta(seconds=30),
                max_staleness_ms=100,
            ),
        ),
    )
    await runtime.write_batch(batch)
    assert sink.writes == []


async def test_realtime_authority_conflict_rejected() -> None:
    runtime, sink = await _runtime(realtime=True)
    now = datetime.now(UTC)
    await runtime.write_batch(
        _batch(
            mode="realtime",
            samples=(_item("p1", "s-1", sample_time=now, max_staleness_ms=5000),),
        )
    )
    foreign = _batch(
        mode="realtime",
        samples=(
            _item(
                "p1",
                "s-2",
                sample_time=now - timedelta(milliseconds=100),
                max_staleness_ms=5000,
            ),
        ),
        collector_id="collector-2",
    )
    with pytest.raises(ValueError, match="multiple realtime publishers"):
        await runtime.write_batch(foreign)
    # The rejected batch must not have reached the target.
    assert len(sink.writes) == 1


async def test_realtime_older_sample_skipped_equaltime_dedup_and_ambiguity() -> None:
    runtime, sink = await _runtime(realtime=True)
    now = datetime.now(UTC)
    await runtime.write_batch(
        _batch(
            mode="realtime",
            samples=(_item("p1", "s-1", sample_time=now, max_staleness_ms=5000),),
        )
    )
    # Older sample_time for the same point is silently superseded.
    await runtime.write_batch(
        _batch(
            mode="realtime",
            batch_id="b-2",
            samples=(
                _item("p1", "s-old", sample_time=now - timedelta(seconds=1), max_staleness_ms=5000),
            ),
        )
    )
    assert len(sink.writes) == 1  # nothing selected -> no write call

    # Equal sample_time + equal sample_id is an idempotent redelivery: deduplicated.
    await runtime.write_batch(
        _batch(
            mode="realtime",
            batch_id="b-3",
            samples=(_item("p1", "s-1", sample_time=now, max_staleness_ms=5000),),
        )
    )
    assert len(sink.writes) == 1

    # Equal sample_time with a different sample_id is ambiguous and must fail.
    with pytest.raises(ValueError, match="ambiguous equal-time"):
        await runtime.write_batch(
            _batch(
                mode="realtime",
                batch_id="b-4",
                samples=(_item("p1", "s-other", sample_time=now, max_staleness_ms=5000),),
            )
        )
    assert len(sink.writes) == 1


async def test_realtime_newer_sample_updates_cache_and_latest_reports_freshness() -> None:
    runtime, sink = await _runtime(realtime=True)
    now = datetime.now(UTC)
    await runtime.write_batch(
        _batch(
            mode="realtime",
            samples=(
                _item(
                    "p1",
                    "s-1",
                    sample_time=now - timedelta(milliseconds=100),
                    max_staleness_ms=5000,
                ),
            ),
        )
    )
    key = ("farm-a", "wtg-01", "p1")
    cached, fresh = runtime.latest(key)
    assert cached is not None and cached.sample_id == "s-1"
    assert fresh is True

    await runtime.write_batch(
        _batch(
            mode="realtime",
            batch_id="b-2",
            samples=(
                _item(
                    "p1",
                    "s-2",
                    sample_time=now - timedelta(milliseconds=50),
                    max_staleness_ms=5000,
                ),
            ),
        )
    )
    cached, _fresh = runtime.latest(key)
    assert cached is not None and cached.sample_id == "s-2"
    assert [value.sample_id for value in sink.writes[1]] == ["s-2"]


async def test_sink_write_failure_is_recorded_and_reraised_with_cache_unchanged() -> None:
    runtime, sink = await _runtime(realtime=False, sink=FakeSink(fail=True))
    now = datetime.now(UTC)
    batch = _batch(
        mode="reliable",
        samples=(_item("p1", "s-1", sample_time=now, max_staleness_ms=None),),
    )
    with pytest.raises(RuntimeError, match="target unavailable"):
        await runtime.write_batch(batch)
    assert runtime.failure == "target unavailable"
    assert sink.writes == []
    assert runtime.last_written is None


async def test_clear_guards_and_realtime_cache_reset() -> None:
    runtime, _sink = await _runtime(realtime=True)
    now = datetime.now(UTC)
    await runtime.write_batch(
        _batch(
            mode="realtime",
            samples=(_item("p1", "s-1", sample_time=now, max_staleness_ms=5000),),
        )
    )
    with pytest.raises(RuntimeError, match="stopped"):
        await runtime.clear()
    await runtime.stop()
    assert runtime.state is SinkState.STOPPED
    cleared = await runtime.clear()
    assert cleared == 0
    cached, fresh = runtime.latest(("farm-a", "wtg-01", "p1"))
    assert cached is None and fresh is False


async def test_reliable_clear_delegates_to_consumer_backlog() -> None:
    consumer = FakeConsumer()
    runtime = SinkRuntime(FakeSink(), consumer, realtime=False)
    with pytest.raises(RuntimeError, match="stopped"):
        runtime.clear_realtime()
    assert await runtime.clear() == 3
    assert consumer.cleared == 1
    assert await runtime.pending() == 7
