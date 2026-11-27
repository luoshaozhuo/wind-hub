"""Kafka Consumer 的退避、连续提交、分区间隔离与代际防护。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiokafka import TopicPartition
from aiokafka.errors import KafkaConnectionError

from storage_service.application.config import load_storage_config
from storage_service.application.ingestion_contract import (
    IngestionBlockedError,
    KafkaRecord,
    WriteUnconfirmedError,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.infrastructure.adapters.outbound.kafka_consumer import (
    KafkaStorageConsumer,
    _AssignedPartition,
)
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflictError,
)

ROOT = Path(__file__).resolve().parents[3]
TOPIC = "windhub-history"
pytestmark = pytest.mark.unit


class _Consumer:
    def __init__(
        self,
        partitions: Sequence[TopicPartition],
        *,
        committed: dict[TopicPartition, int | None] | None = None,
        earliest: dict[TopicPartition, int] | None = None,
        end: dict[TopicPartition, int] | None = None,
    ) -> None:
        self._assignment = set(partitions)
        self.committed_offsets = committed or {}
        self.earliest = earliest or {tp: 0 for tp in partitions}
        self.end = end or {
            tp: max(self.earliest[tp], self.committed_offsets.get(tp) or 0) for tp in partitions
        }
        self.seeks: list[tuple[TopicPartition, int]] = []
        self.commits: list[dict[TopicPartition, int]] = []
        self.commit_errors: list[Exception] = []
        self.paused: list[TopicPartition] = []
        self.resumed: list[TopicPartition] = []
        self.commit_started: asyncio.Event | None = None
        self.commit_release: asyncio.Event | None = None
        self.stopped = False

    def assignment(self) -> set[TopicPartition]:
        return set(self._assignment)

    def pause(self, tp: TopicPartition) -> None:
        self.paused.append(tp)

    def resume(self, tp: TopicPartition) -> None:
        self.resumed.append(tp)

    def seek(self, tp: TopicPartition, offset: int) -> None:
        self.seeks.append((tp, offset))

    async def committed(self, tp: TopicPartition) -> int | None:
        return self.committed_offsets.get(tp)

    async def beginning_offsets(
        self, partitions: Sequence[TopicPartition]
    ) -> dict[TopicPartition, int]:
        return {tp: self.earliest[tp] for tp in partitions}

    async def end_offsets(self, partitions: Sequence[TopicPartition]) -> dict[TopicPartition, int]:
        return {tp: self.end[tp] for tp in partitions}

    async def commit(self, offsets: dict[TopicPartition, int]) -> None:
        self.commits.append(dict(offsets))
        if self.commit_errors:
            raise self.commit_errors.pop(0)
        if self.commit_started is not None:
            self.commit_started.set()
        if self.commit_release is not None:
            await self.commit_release.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._assignment.clear()


class _Metadata:
    def __init__(self) -> None:
        self.blocked: dict[tuple[str, int], tuple[int, str, bytes]] = {}
        self.accept_retry = True
        self.retry_requests: list[tuple[str, str, int, int]] = []

    def get_blocked_partition(self, topic: str, partition: int) -> tuple[int, str] | None:
        blocked = self.blocked.get((topic, partition))
        return (blocked[0], blocked[1]) if blocked is not None else None

    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        self.blocked[(topic, partition)] = (offset, reason, payload)

    def request_partition_retry(
        self, *, request_id: str, topic: str, partition: int, expected_offset: int
    ) -> bool:
        self.retry_requests.append((request_id, topic, partition, expected_offset))
        if self.accept_retry:
            self.blocked.pop((topic, partition), None)
        return self.accept_retry


class _Ingestion:
    def __init__(self, failures: Sequence[Exception] = ()) -> None:
        self.failures = list(failures)
        self.calls: list[tuple[int, list[int]]] = []

    async def process(
        self, *, generation: int, offsets: PartitionOffsets, records: Sequence[KafkaRecord]
    ) -> None:
        self.calls.append((generation, [record.offset for record in records]))
        if self.failures:
            raise self.failures.pop(0)
        for record in records:
            offsets.observe(generation=generation, offset=record.offset)
            offsets.confirm(generation=generation, offset=record.offset)


def _record(tp: TopicPartition, offset: int, payload: bytes = b"record") -> KafkaRecord:
    return KafkaRecord(
        topic=tp.topic,
        partition=tp.partition,
        offset=offset,
        key=None,
        value=payload,
    )


def _build(
    monkeypatch: pytest.MonkeyPatch,
    broker: _Consumer,
    ingestion: object,
    *,
    metadata: _Metadata | None = None,
    retry: dict[str, int] | None = None,
    commit_timeout_ms: int | None = None,
) -> KafkaStorageConsumer:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    if retry is not None:
        config = config.model_copy(update={"retry": config.retry.model_copy(update=retry)})
    if commit_timeout_ms is not None:
        config = config.model_copy(
            update={
                "kafka": config.kafka.model_copy(update={"commit_timeout_ms": commit_timeout_ms})
            }
        )
    monkeypatch.setattr(
        "storage_service.infrastructure.adapters.outbound.kafka_consumer.AIOKafkaConsumer",
        lambda **_kwargs: broker,
    )
    return KafkaStorageConsumer(
        configuration=config,
        bootstrap_servers="127.0.0.1:9092",
        ingestion=cast(Any, ingestion),
        metadata=cast(Any, metadata or _Metadata()),
        metrics=StorageRuntimeMetrics(),
    )


def _own(
    consumer: KafkaStorageConsumer,
    tp: TopicPartition,
    *,
    generation: int = 1,
    cursor: int = 0,
) -> _AssignedPartition:
    state = _AssignedPartition(
        offsets=PartitionOffsets(
            partition=tp.partition,
            generation=generation,
            next_commit_offset=cursor,
        ),
        generation=generation,
    )
    consumer._assignments[tp] = state
    return state


def _instant_backoff(monkeypatch: pytest.MonkeyPatch, delays: list[float]) -> None:
    async def wait_for(awaitable: Coroutine[Any, Any, bool], timeout: float) -> bool:
        delays.append(timeout)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", wait_for)


async def test_retry_uses_backoff_and_honors_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    ingestion = _Ingestion([WriteUnconfirmedError("unknown")] * 3)
    consumer = _build(
        monkeypatch,
        broker,
        ingestion,
        retry={"initial_ms": 500, "max_ms": 1000, "max_attempts": 2},
    )
    _own(consumer, tp)
    delays: list[float] = []
    _instant_backoff(monkeypatch, delays)

    for _ in range(2):
        await consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))
    with pytest.raises(WriteUnconfirmedError):
        await consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))

    assert delays == [0.5, 1.0]
    assert broker.seeks == [(tp, 0), (tp, 0)]
    assert broker.commits == []
    assert consumer._retry_failures[tp] == 3


async def test_unlimited_retry_is_bounded_and_success_resets_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    ingestion = _Ingestion([WriteUnconfirmedError("unknown")] * 40)
    consumer = _build(
        monkeypatch,
        broker,
        ingestion,
        retry={"initial_ms": 100, "max_ms": 1000, "max_attempts": 0},
    )
    _own(consumer, tp)
    delays: list[float] = []
    _instant_backoff(monkeypatch, delays)

    for _ in range(40):
        await consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))
    assert delays[-1] == 1.0
    assert consumer._retry_failures[tp] == 40

    await consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))
    assert consumer._retry_failures == {}
    assert broker.commits == [{tp: 1}]
    assert broker.seeks == [(tp, 0)] * 40


async def test_commit_failure_replays_original_cursor_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    broker.commit_errors = [KafkaConnectionError("commit acknowledgement lost")]
    consumer = _build(monkeypatch, broker, _Ingestion())
    state = _own(consumer, tp, cursor=4)
    _instant_backoff(monkeypatch, [])

    await consumer._process_partition(tp, [_record(tp, 4)], asyncio.Semaphore(1))

    assert broker.commits == [{tp: 5}]
    assert broker.seeks == [(tp, 4)]
    assert state.offsets.next_commit_offset == 4
    assert consumer._retry_failures == {tp: 1}

    await consumer._process_partition(tp, [_record(tp, 4)], asyncio.Semaphore(1))

    assert broker.commits == [{tp: 5}, {tp: 5}]
    assert broker.seeks == [(tp, 4)]
    assert consumer._retry_failures == {}


async def test_commit_timeout_replays_original_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    broker.commit_release = asyncio.Event()
    consumer = _build(monkeypatch, broker, _Ingestion(), commit_timeout_ms=100)
    state = _own(consumer, tp, cursor=4)
    _instant_backoff(monkeypatch, [])

    await consumer._process_partition(tp, [_record(tp, 4)], asyncio.Semaphore(1))

    assert broker.commits == [{tp: 5}]
    assert broker.seeks == [(tp, 4)]
    assert state.offsets.next_commit_offset == 4
    assert consumer._retry_failures == {tp: 1}


async def test_retry_rewinds_to_earliest_unconfirmed_not_batch_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)

    class _PartialUnknown:
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            for record in records[:2]:
                offsets.observe(generation=generation, offset=record.offset)
                offsets.confirm(generation=generation, offset=record.offset)
            raise WriteUnconfirmedError("tail is unknown")

    broker = _Consumer([tp])
    consumer = _build(monkeypatch, broker, _PartialUnknown())
    _own(consumer, tp, cursor=5)
    _instant_backoff(monkeypatch, [])

    await consumer._process_partition(
        tp,
        [_record(tp, 5), _record(tp, 6), _record(tp, 7)],
        asyncio.Semaphore(1),
    )

    assert broker.seeks == [(tp, 7)]
    assert consumer._assignments[tp].offsets.next_commit_offset == 7
    assert consumer._assignments[tp].offsets.highest_delivered_offset == 6
    assert broker.commits == []


async def test_rebalance_during_processing_blocks_old_commit_and_seek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    started = asyncio.Event()
    release = asyncio.Event()

    class _GatedSuccess:
        async def process(self, **_kwargs: object) -> None:
            started.set()
            await release.wait()

    broker = _Consumer([tp])
    consumer = _build(monkeypatch, broker, _GatedSuccess())
    old = _own(consumer, tp)
    task = asyncio.create_task(
        consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))
    )
    await started.wait()
    consumer.revoke({tp})
    replacement = _own(consumer, tp, generation=2)
    release.set()
    await task

    assert broker.commits == []
    assert broker.seeks == []
    assert consumer._assignments[tp] is replacement
    assert not old.offsets._active


async def test_rebalance_during_commit_blocks_old_seek_and_state_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    broker.commit_started = asyncio.Event()
    broker.commit_release = asyncio.Event()
    consumer = _build(monkeypatch, broker, _Ingestion())
    _own(consumer, tp)
    task = asyncio.create_task(
        consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))
    )
    await broker.commit_started.wait()
    consumer.revoke({tp})
    replacement = _own(consumer, tp, generation=2)
    replacement_offsets = replacement.offsets
    consumer._retry_failures[tp] = 4
    broker.commit_release.set()
    await task

    assert broker.commits == [{tp: 1}]
    assert broker.seeks == []
    assert consumer._retry_failures == {tp: 4}
    assert replacement.offsets is replacement_offsets


async def test_old_generation_failure_cannot_restore_retry_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    started = asyncio.Event()
    release = asyncio.Event()

    class _GatedUnknown:
        async def process(self, **_kwargs: object) -> None:
            started.set()
            await release.wait()
            raise WriteUnconfirmedError("old owner result")

    broker = _Consumer([tp])
    consumer = _build(monkeypatch, broker, _GatedUnknown())
    _own(consumer, tp)
    _instant_backoff(monkeypatch, [])
    task = asyncio.create_task(
        consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))
    )
    await started.wait()
    consumer.revoke({tp})
    replacement = _own(consumer, tp, generation=2)
    replacement_offsets = replacement.offsets
    release.set()
    await task

    assert consumer._retry_failures == {}
    assert broker.seeks == []
    assert replacement.offsets is replacement_offsets


async def test_stop_during_backoff_wakes_without_rewind_after_reassignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    started = asyncio.Event()

    class _Unknown:
        async def process(self, **_kwargs: object) -> None:
            started.set()
            raise WriteUnconfirmedError("unknown")

    broker = _Consumer([tp])
    consumer = _build(
        monkeypatch,
        broker,
        _Unknown(),
        retry={"initial_ms": 60000, "max_ms": 60000, "max_attempts": 0},
    )
    _own(consumer, tp)
    task = asyncio.create_task(
        consumer._process_partition(tp, [_record(tp, 0)], asyncio.Semaphore(1))
    )
    await started.wait()
    await asyncio.sleep(0)
    consumer.request_stop()
    consumer.revoke({tp})
    replacement = _own(consumer, tp, generation=2)
    await asyncio.wait_for(task, timeout=0.2)

    assert broker.seeks == []
    assert broker.commits == []
    assert consumer._assignments[tp] is replacement


async def test_assignment_restores_committed_fetch_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp], committed={tp: 5}, earliest={tp: 2})
    consumer = _build(monkeypatch, broker, _Ingestion())

    await consumer.assign({tp})

    assert broker.seeks == [(tp, 5)]
    assert consumer._assignments[tp].offsets.next_commit_offset == 5


async def test_assignment_rejects_bootstrap_and_retention_offset_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = TopicPartition(TOPIC, 0)
    broker = _Consumer([missing], committed={missing: None}, earliest={missing: 7})
    consumer = _build(monkeypatch, broker, _Ingestion())
    with pytest.raises(RuntimeError, match="OFFSET_GAP"):
        await consumer.assign({missing})

    trimmed = TopicPartition(TOPIC, 1)
    broker = _Consumer([trimmed], committed={trimmed: 3}, earliest={trimmed: 4})
    consumer = _build(monkeypatch, broker, _Ingestion())
    with pytest.raises(RuntimeError, match="OFFSET_GAP"):
        await consumer.assign({trimmed})
    assert broker.seeks == []

    recreated = TopicPartition(TOPIC, 2)
    broker = _Consumer([recreated], committed={recreated: 1000}, end={recreated: 0})
    consumer = _build(monkeypatch, broker, _Ingestion())
    with pytest.raises(RuntimeError, match="OFFSET_GAP"):
        await consumer.assign({recreated})
    assert broker.seeks == []


async def test_schema_conflict_isolated_and_retry_resumes_contiguous_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = TopicPartition(TOPIC, 0)
    healthy = TopicPartition(TOPIC, 1)

    class _PartitionFailure:
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            if offsets.partition == failed.partition:
                raise TDengineSchemaConflictError("incompatible physical schema")
            for record in records:
                offsets.observe(generation=generation, offset=record.offset)
                offsets.confirm(generation=generation, offset=record.offset)

    broker = _Consumer([failed, healthy])
    metadata = _Metadata()
    consumer = _build(monkeypatch, broker, _PartitionFailure(), metadata=metadata)
    failed_state = _own(consumer, failed, cursor=4)
    _own(consumer, healthy, cursor=8)

    await asyncio.gather(
        consumer._process_partition(
            failed, [_record(failed, 4, b"bad"), _record(failed, 5)], asyncio.Semaphore(2)
        ),
        consumer._process_partition(healthy, [_record(healthy, 8)], asyncio.Semaphore(2)),
    )

    assert failed_state.blocked
    assert broker.paused == [failed]
    assert metadata.blocked[(TOPIC, 0)][:2] == (4, "TDENGINE_SCHEMA_CONFLICT")
    assert broker.commits == [{healthy: 9}]
    assert consumer._metrics.snapshot().schema_conflicts == 1

    consumer._retry_failures[failed] = 2
    accepted = await consumer.retry_blocked_partition(
        request_id="retry-1", topic=TOPIC, partition=0, expected_offset=4
    )
    assert accepted
    assert not failed_state.blocked
    assert failed_state.offsets.next_commit_offset == 4
    assert consumer._retry_failures == {}
    assert broker.seeks[-1] == (failed, 4)
    assert broker.resumed == [failed]


async def test_retry_rejected_by_durable_evidence_does_not_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    metadata = _Metadata()
    metadata.accept_retry = False
    consumer = _build(monkeypatch, broker, _Ingestion(), metadata=metadata)
    state = _own(consumer, tp, cursor=3)
    state.blocked = True

    accepted = await consumer.retry_blocked_partition(
        request_id="stale", topic=TOPIC, partition=0, expected_offset=3
    )

    assert not accepted
    assert state.blocked
    assert broker.seeks == []
    assert broker.resumed == []


async def test_partition_fifo_with_cross_partition_parallelism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = TopicPartition(TOPIC, 0)
    second = TopicPartition(TOPIC, 1)
    entered: set[int] = set()
    release = asyncio.Event()
    orders: dict[int, list[int]] = {0: [], 1: []}
    active = 0
    maximum_active = 0

    class _ParallelIngestion:
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            entered.add(offsets.partition)
            if len(entered) == 2:
                release.set()
            await release.wait()
            for record in records:
                offsets.observe(generation=generation, offset=record.offset)
                offsets.confirm(generation=generation, offset=record.offset)
                orders[offsets.partition].append(record.offset)
            active -= 1

    broker = _Consumer([first, second])
    consumer = _build(monkeypatch, broker, _ParallelIngestion())
    _own(consumer, first)
    _own(consumer, second)
    semaphore = asyncio.Semaphore(2)

    await asyncio.gather(
        consumer._process_partition(
            first, [_record(first, 0), _record(first, 1), _record(first, 2)], semaphore
        ),
        consumer._process_partition(second, [_record(second, 0), _record(second, 1)], semaphore),
    )

    assert maximum_active == 2
    assert orders == {0: [0, 1, 2], 1: [0, 1]}
    assert {item for commit in broker.commits for item in commit.items()} == {
        (first, 3),
        (second, 2),
    }


async def test_ingestion_block_does_not_pause_other_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = TopicPartition(TOPIC, 0)
    healthy = TopicPartition(TOPIC, 1)

    class _Blocked:
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            if offsets.partition == 0:
                raise IngestionBlockedError("bad record")
            for record in records:
                offsets.observe(generation=generation, offset=record.offset)
                offsets.confirm(generation=generation, offset=record.offset)

    broker = _Consumer([failed, healthy])
    consumer = _build(monkeypatch, broker, _Blocked())
    failed_state = _own(consumer, failed)
    _own(consumer, healthy)

    await asyncio.gather(
        consumer._process_partition(failed, [_record(failed, 0)], asyncio.Semaphore(2)),
        consumer._process_partition(healthy, [_record(healthy, 0)], asyncio.Semaphore(2)),
    )

    assert failed_state.blocked
    assert broker.paused == [failed]
    assert broker.commits == [{healthy: 1}]


async def test_successive_full_batches_keep_prefetched_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    consumer = _build(monkeypatch, broker, _Ingestion())
    state = _own(consumer, tp)
    semaphore = asyncio.Semaphore(1)
    for start in (0, 3):
        await consumer._process_partition(
            tp, [_record(tp, offset) for offset in range(start, start + 3)], semaphore
        )
    assert broker.seeks == []
    assert broker.commits == [{tp: 3}, {tp: 6}]
    assert state.offsets.next_commit_offset == 6


async def test_run_polls_next_batch_before_first_write_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded poll queue must overlap Kafka fetching with TDengine I/O."""
    tp = TopicPartition(TOPIC, 0)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_polled = asyncio.Event()

    class _PollingConsumer(_Consumer):
        def __init__(self) -> None:
            super().__init__([tp])
            self.polls = 0

        async def getmany(self, **_kwargs: object) -> dict[TopicPartition, list[object]]:
            self.polls += 1
            if self.polls <= 2:
                if self.polls == 2:
                    second_polled.set()
                offset = self.polls - 1
                return {
                    tp: [
                        SimpleNamespace(
                            topic=tp.topic,
                            partition=tp.partition,
                            offset=offset,
                            key=None,
                            value=b"record",
                        )
                    ]
                }
            await asyncio.sleep(0.01)
            return {}

    class _SlowIngestion(_Ingestion):
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            if records[0].offset == 0:
                first_started.set()
                await release_first.wait()
            await super().process(generation=generation, offsets=offsets, records=records)

    broker = _PollingConsumer()
    ingestion = _SlowIngestion()
    consumer = _build(monkeypatch, broker, ingestion)
    _own(consumer, tp)
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2)
        await asyncio.wait_for(second_polled.wait(), timeout=2)
        assert broker.commits == []
        release_first.set()
        async with asyncio.timeout(2):
            while len(broker.commits) != 2:
                await asyncio.sleep(0.01)
        assert broker.commits == [{tp: 1}, {tp: 2}]
        assert ingestion.calls == [(1, [0]), (1, [1])]
    finally:
        release_first.set()
        consumer.request_stop()
        await asyncio.wait_for(task, timeout=2)


async def test_queued_suffix_discarded_after_rebalance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued old-generation data must never be committed by the new owner."""
    tp = TopicPartition(TOPIC, 0)
    started = asyncio.Event()
    release = asyncio.Event()
    second_polled = asyncio.Event()

    class _PollingConsumer(_Consumer):
        def __init__(self) -> None:
            super().__init__([tp])
            self.polls = 0

        async def getmany(self, **_kwargs: object) -> dict[TopicPartition, list[object]]:
            self.polls += 1
            if self.polls <= 2:
                if self.polls == 2:
                    second_polled.set()
                return {
                    tp: [
                        SimpleNamespace(
                            topic=tp.topic,
                            partition=tp.partition,
                            offset=self.polls - 1,
                            key=None,
                            value=b"record",
                        )
                    ]
                }
            await asyncio.sleep(0.01)
            return {}

    class _Gated(_Ingestion):
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            started.set()
            await release.wait()
            await super().process(generation=generation, offsets=offsets, records=records)

    broker = _PollingConsumer()
    ingestion = _Gated()
    consumer = _build(monkeypatch, broker, ingestion)
    _own(consumer, tp)
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(second_polled.wait(), timeout=2)
        consumer.revoke({tp})
        _own(consumer, tp, generation=2)
        release.set()
        await asyncio.sleep(0.05)
        assert broker.commits == []
        assert ingestion.calls == [(1, [0])]
    finally:
        release.set()
        consumer.request_stop()
        await asyncio.wait_for(task, timeout=2)


async def test_run_coalesces_contiguous_buffered_poll_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an earlier write stalls, buffered later polls share one batch."""
    tp = TopicPartition(TOPIC, 0)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    third_polled = asyncio.Event()

    class _PollingConsumer(_Consumer):
        def __init__(self) -> None:
            super().__init__([tp])
            self.polls = 0

        async def getmany(self, **_kwargs: object) -> dict[TopicPartition, list[object]]:
            self.polls += 1
            if self.polls <= 3:
                if self.polls == 3:
                    third_polled.set()
                return {
                    tp: [
                        SimpleNamespace(
                            topic=tp.topic,
                            partition=tp.partition,
                            offset=self.polls - 1,
                            key=None,
                            value=b"record",
                        )
                    ]
                }
            await asyncio.sleep(0.01)
            return {}

    class _Gated(_Ingestion):
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            if records[0].offset == 0:
                first_started.set()
                await release_first.wait()
            await super().process(generation=generation, offsets=offsets, records=records)

    broker = _PollingConsumer()
    ingestion = _Gated()
    consumer = _build(monkeypatch, broker, ingestion)
    _own(consumer, tp)
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2)
        await asyncio.wait_for(third_polled.wait(), timeout=2)
        async with asyncio.timeout(2):
            while consumer._metrics.snapshot().buffer_rows < 3:
                await asyncio.sleep(0)
        release_first.set()
        async with asyncio.timeout(2):
            while len(broker.commits) != 2:
                await asyncio.sleep(0.01)
        assert ingestion.calls == [(1, [0]), (1, [1, 2])]
        assert broker.commits == [{tp: 1}, {tp: 3}]
    finally:
        release_first.set()
        consumer.request_stop()
        await asyncio.wait_for(task, timeout=2)


async def test_dispatcher_failure_releases_queued_buffer_and_closes_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fatal processing error must not retain uncommitted prefetched rows."""
    tp = TopicPartition(TOPIC, 0)
    writing = asyncio.Event()
    release = asyncio.Event()
    fetched_three = asyncio.Event()

    class _PollingConsumer(_Consumer):
        def __init__(self) -> None:
            super().__init__([tp])
            self.polls = 0

        async def getmany(self, **_kwargs: object) -> dict[TopicPartition, list[object]]:
            self.polls += 1
            if self.polls <= 3:
                if self.polls == 3:
                    fetched_three.set()
                return {
                    tp: [
                        SimpleNamespace(
                            topic=tp.topic,
                            partition=tp.partition,
                            offset=self.polls - 1,
                            key=None,
                            value=b"record",
                        )
                    ]
                }
            await asyncio.sleep(0.01)
            return {}

    class _FailingIngestion:
        async def process(self, **_kwargs: object) -> None:
            writing.set()
            await release.wait()
            raise RuntimeError("injected fatal processing error")

    broker = _PollingConsumer()
    consumer = _build(monkeypatch, broker, _FailingIngestion())
    _own(consumer, tp)
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(writing.wait(), timeout=2)
        await asyncio.wait_for(fetched_three.wait(), timeout=2)
        async with asyncio.timeout(2):
            while consumer._metrics.snapshot().buffer_rows < 3:
                await asyncio.sleep(0)
        release.set()
        with pytest.raises(ExceptionGroup) as raised:
            await asyncio.wait_for(task, timeout=2)
        assert any(
            isinstance(exc, RuntimeError) and "injected fatal" in str(exc)
            for exc in raised.value.exceptions
        )
        snapshot = consumer._metrics.snapshot()
        assert snapshot.buffer_rows == 0
        assert snapshot.buffer_bytes == 0
        assert broker.stopped
        assert broker.commits == []
    finally:
        release.set()
        if not task.done():
            consumer.request_stop()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)


async def test_normal_stop_drains_admitted_batches_before_kafka_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graceful STOP may not discard a queued, durably writable suffix."""
    tp = TopicPartition(TOPIC, 0)
    writing = asyncio.Event()
    release = asyncio.Event()
    fetched_two = asyncio.Event()

    class _PollingConsumer(_Consumer):
        def __init__(self) -> None:
            super().__init__([tp])
            self.polls = 0

        async def getmany(self, **_kwargs: object) -> dict[TopicPartition, list[object]]:
            self.polls += 1
            if self.polls <= 2:
                if self.polls == 2:
                    fetched_two.set()
                return {
                    tp: [
                        SimpleNamespace(
                            topic=tp.topic,
                            partition=tp.partition,
                            offset=self.polls - 1,
                            key=None,
                            value=b"record",
                        )
                    ]
                }
            await asyncio.sleep(0.01)
            return {}

    class _GatedIngestion(_Ingestion):
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            if records[0].offset == 0:
                writing.set()
                await release.wait()
            await super().process(generation=generation, offsets=offsets, records=records)

    broker = _PollingConsumer()
    ingestion = _GatedIngestion()
    consumer = _build(monkeypatch, broker, ingestion)
    _own(consumer, tp)
    runner = asyncio.create_task(consumer.run())
    stopper: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(writing.wait(), timeout=2)
        await asyncio.wait_for(fetched_two.wait(), timeout=2)
        async with asyncio.timeout(2):
            while consumer._metrics.snapshot().buffer_rows < 2:
                await asyncio.sleep(0)
        stopper = asyncio.create_task(consumer.stop())
        await asyncio.sleep(0)
        assert not stopper.done()
        assert not broker.stopped
        release.set()
        await asyncio.wait_for(stopper, timeout=2)
        await asyncio.wait_for(runner, timeout=2)
        assert broker.stopped
        assert broker.commits == [{tp: 1}, {tp: 2}]
        assert ingestion.calls == [(1, [0]), (1, [1])]
        assert consumer._metrics.snapshot().buffer_rows == 0
    finally:
        release.set()
        consumer.request_stop()
        if stopper is not None and not stopper.done():
            await asyncio.gather(stopper, return_exceptions=True)
        if not runner.done():
            await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), timeout=2)


async def test_prefetched_batch_rebalance_while_waiting_for_worker_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not reinterpret old queued records using a replacement generation."""
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    ingestion = _Ingestion()
    consumer = _build(monkeypatch, broker, ingestion)
    old = _own(consumer, tp, generation=1)
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    task = asyncio.create_task(
        consumer._process_partition(
            tp,
            [_record(tp, 0)],
            semaphore,
            expected_owner=old,
        )
    )
    await asyncio.sleep(0)
    consumer.revoke({tp})
    replacement = _own(consumer, tp, generation=2)
    semaphore.release()
    await asyncio.wait_for(task, timeout=2)
    assert consumer._assignments[tp] is replacement
    assert ingestion.calls == []
    assert broker.commits == []


async def test_prefetched_batch_recheck_cursor_after_worker_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued suffix cannot bypass a retry rewind while awaiting its permit."""
    tp = TopicPartition(TOPIC, 0)
    broker = _Consumer([tp])
    ingestion = _Ingestion()
    consumer = _build(monkeypatch, broker, ingestion)
    owner = _own(consumer, tp, cursor=3)
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    task = asyncio.create_task(
        consumer._process_partition(
            tp, [_record(tp, 3)], semaphore, expected_owner=owner
        )
    )
    await asyncio.sleep(0)
    owner.offsets = PartitionOffsets(
        partition=tp.partition, generation=owner.generation, next_commit_offset=2
    )
    semaphore.release()
    await asyncio.wait_for(task, timeout=2)
    assert ingestion.calls == []
    assert broker.commits == []


async def test_run_applies_row_backpressure_while_writer_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Poll-ahead must stop at the configured row budget."""
    tp = TopicPartition(TOPIC, 0)
    first_started = asyncio.Event()
    release = asyncio.Event()

    class _UnlimitedBroker(_Consumer):
        def __init__(self) -> None:
            super().__init__([tp])
            self.polls = 0

        async def getmany(self, **_kwargs: object) -> dict[TopicPartition, list[object]]:
            offset = self.polls
            self.polls += 1
            return {
                tp: [
                    SimpleNamespace(
                        topic=tp.topic,
                        partition=tp.partition,
                        offset=offset,
                        key=None,
                        value=b"record",
                    )
                ]
            }

    class _Gated(_Ingestion):
        async def process(
            self,
            *,
            generation: int,
            offsets: PartitionOffsets,
            records: Sequence[KafkaRecord],
        ) -> None:
            if records[0].offset == 0:
                first_started.set()
                await release.wait()
            await super().process(generation=generation, offsets=offsets, records=records)

    broker = _UnlimitedBroker()
    consumer = _build(monkeypatch, broker, _Gated())
    limits = consumer._config.tdengine.ingestion.model_copy(
        update={"buffer_max_rows": 100, "batch_max_rows": 100}
    )
    tdengine = consumer._config.tdengine.model_copy(update={"ingestion": limits})
    kafka = consumer._config.kafka.model_copy(update={"max_poll_records": 1})
    consumer._config = consumer._config.model_copy(
        update={"kafka": kafka, "tdengine": tdengine}
    )
    _own(consumer, tp)
    runner = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(first_started.wait(), timeout=2)
        async with asyncio.timeout(2):
            while consumer._metrics.snapshot().buffer_rows < 100:
                await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        assert broker.polls == 100
        assert consumer._metrics.snapshot().buffer_rows == 100
        assert broker.commits == []
    finally:
        consumer.request_stop()
        release.set()
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), timeout=3)
    assert consumer._metrics.snapshot().buffer_rows == 0


def test_storage_shutdown_drain_deadline_is_bounded() -> None:
    """Graceful stop timeout is a validated service contract, not a magic 10s."""
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    assert config.kafka.shutdown_drain_timeout_ms == 120000
    values = config.kafka.model_dump()
    values["shutdown_drain_timeout_ms"] = 0
    with pytest.raises(ValueError):
        type(config.kafka).model_validate(values)
    values["shutdown_drain_timeout_ms"] = 600001
    with pytest.raises(ValueError):
        type(config.kafka).model_validate(values)
