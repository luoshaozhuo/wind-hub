"""KafkaOutput device-lock stripe concurrency semantics (unit, fake producer).

Covers design §6.3.3: the same device stripe serializes while distinct
partitions overlap; a hash collision only reduces parallelism; a mid-batch failure still
awaits the in-flight confirmation (no orphan future); close/reopen leaves no
pending confirmation and unopened writes fail closed.

The durable Kafka boundary is ``write_encoded_batch`` (one pre-encoded record
batch per device key/stripe); the fake producer implements the batch-builder
API the production adapter drives.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from zlib import crc32

import aiokafka
import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.infrastructure.adapters.outbound.kafka_wire import (
    encode_kafka_wide,
    encode_kafka_wide_key,
)
from sink_service.infrastructure.adapters.outbound.realtime_adapters import (
    KafkaOutput,
)

pytestmark = [pytest.mark.unit, pytest.mark.reliability]

FARM = "farm-a"
COLLECTOR = "collector-1"
TOPIC = "qa-topic"
STRIPES = 128


class _Cluster:
    def __init__(self) -> None:
        self.listeners: list[Any] = []

    def add_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: Any) -> None:
        self.listeners.remove(listener)


class _Client:
    def __init__(self) -> None:
        self.cluster = _Cluster()

    async def _wait_on_metadata(self, topic: str) -> None:
        return None


class _BatchBuilder:
    def __init__(self) -> None:
        self.records: list[tuple[bytes, bytes]] = []
        self.closed = False

    def append(self, *, key: bytes, value: bytes, timestamp: Any) -> object:
        self.records.append((key, value))
        return object()  # truthy: record accepted into the current batch

    def close(self) -> None:
        self.closed = True


class _Future:
    def __init__(self) -> None:
        self.awaited = 0
        self._event = asyncio.Event()

    def set_result(self) -> None:
        self._event.set()

    def __await__(self) -> Any:
        self.awaited += 1
        yield from self._event.wait().__await__()


class _BatchProducer:
    """Gated batch producer tracking in-flight send_batch calls per stripe."""

    def __init__(
        self,
        *,
        gate: asyncio.Event | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.client = _Client()
        self.gate = gate
        self.fail_after = fail_after
        self.inflight = 0
        self.max_inflight = 0
        self.send_order: list[str] = []
        self.futures: list[_Future] = []
        self._sends = 0
        self.stopped = False
        self.partitions: dict[bytes, int] = {}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True

    async def _wait_on_metadata(self, topic: str) -> None:
        return None

    def _partition(
        self,
        topic: str,
        partition: Any,
        key: bytes,
        value: Any,
        serialized_key: bytes,
        serialized_value: Any,
    ) -> int:
        return self.partitions.get(serialized_key, 0)

    def create_batch(self) -> _BatchBuilder:
        return _BatchBuilder()

    async def send_batch(self, builder: _BatchBuilder, topic: str, partition: Any) -> _Future:
        device = builder.records[0][0].decode().split(":")[-1] if builder.records else "?"
        self._sends += 1
        if self.fail_after is not None and self._sends > self.fail_after:
            raise RuntimeError("produce refused")
        future = _Future()
        self.futures.append(future)
        self.send_order.append(device)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            future.set_result()
            return future
        finally:
            self.inflight -= 1


def _patch_producer(monkeypatch: pytest.MonkeyPatch, producer: _BatchProducer) -> None:
    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", lambda **options: producer)


def _output() -> KafkaOutput:
    return KafkaOutput(servers=["127.0.0.1:9092"], topic=TOPIC, timeout_ms=10000)


def _encoded(device: str, count: int = 1) -> tuple[bytes, ...]:
    """Build independent complete device cycles for Kafka adapter fault tests."""
    now = datetime.now(UTC)
    records: list[bytes] = []
    for index in range(count):
        item = SampleItem(
            point_id="power",
            sample_id=f"s-{device}-{index}",
            value=float(index + 1),
            quality="good",
            sample_time=now,
        )
        batch = SampleBatch(
            batch_id=f"b-stripe-{index}",
            mode="reliable",
            wind_farm_id=FARM,
            collector_id=COLLECTOR,
            task_id="task-1",
            device_type="wind_turbine",
            device_id=device,
            point_table_id="point_table_wtg_sim_v1.0.0",
            device_config_id="device_farm-a_v1.0.0",
            sample_time=now,
            publish_time=now,
            samples=(item,),
        )
        records.append(
            encode_kafka_wide(
                batch,
                batch.samples,
                device_config_id="device_farm-a_v1.0.0",
                physical_points=("power",),
                data_level="protocol",
            )
        )
    return tuple(records)


def _route(device: str) -> tuple[bytes, int]:
    key = encode_kafka_wide_key(FARM, device)
    return key, crc32(key) % STRIPES


def test_device_locks_are_bounded_stripes() -> None:
    output = _output()
    assert len(output._device_locks) == STRIPES  # noqa: SLF001
    assert all(isinstance(lock, asyncio.Lock) for lock in output._device_locks)  # noqa: SLF001


async def test_distinct_partitions_overlap_and_each_fully_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    producer = _BatchProducer(gate=gate)
    _patch_producer(monkeypatch, producer)
    output = _output()
    await output.open()
    try:
        key_a, stripe_a = _route("DEV-A")
        key_b, stripe_b = _route("DEV-B")
        producer.partitions[key_b] = 1
        task_a = asyncio.create_task(
            output.write_encoded_batch(_encoded("DEV-A", 3), key=key_a, stripe=stripe_a)
        )
        task_b = asyncio.create_task(
            output.write_encoded_batch(_encoded("DEV-B", 3), key=key_b, stripe=stripe_b)
        )
        for _ in range(40):
            await asyncio.sleep(0.005)
            if producer.max_inflight == 2:
                break
        assert producer.max_inflight == 2  # distinct partitions overlap
        gate.set()
        outcome_a, outcome_b = await asyncio.gather(task_a, task_b)
        # One batch confirmation settles all three encoded records.
        assert outcome_a.delivered == (0, 1, 2)
        assert outcome_b.delivered == (0, 1, 2)
        assert sorted(producer.send_order) == ["DEV-A", "DEV-B"]
        assert all(future.awaited == 1 for future in producer.futures)
    finally:
        await output.close()


async def test_stripe_hash_collision_serializes_without_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tests.sink_service.unit.test_kafka_device_stripes.crc32", lambda _key: 0)
    gate = asyncio.Event()
    producer = _BatchProducer(gate=gate)
    _patch_producer(monkeypatch, producer)
    output = _output()
    await output.open()
    try:
        key_a, stripe_a = _route("DEV-A")
        key_b, stripe_b = _route("DEV-B")
        task_a = asyncio.create_task(
            output.write_encoded_batch(_encoded("DEV-A"), key=key_a, stripe=stripe_a)
        )
        for _ in range(40):
            await asyncio.sleep(0.005)
            if producer.inflight == 1:
                break
        task_b = asyncio.create_task(
            output.write_encoded_batch(_encoded("DEV-B"), key=key_b, stripe=stripe_b)
        )
        await asyncio.sleep(0.05)
        assert producer.max_inflight == 1  # collision: parallelism degrades to 1
        gate.set()
        outcome_a, outcome_b = await asyncio.gather(task_a, task_b)
        assert outcome_a.delivered == (0,) and outcome_b.delivered == (0,)
        assert all(future.awaited == 1 for future in producer.futures)
    finally:
        await output.close()


async def test_send_failure_reports_error_without_false_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = _BatchProducer(fail_after=1)
    _patch_producer(monkeypatch, producer)
    output = _output()
    await output.open()
    try:
        key, stripe = _route("DEV-A")
        first = await output.write_encoded_batch(_encoded("DEV-A"), key=key, stripe=stripe)
        assert first.delivered == (0,)
        # Second batch fails at enqueue; no success is fabricated.
        failed = await output.write_encoded_batch(_encoded("DEV-A"), key=key, stripe=stripe)
        assert failed.delivered == ()
        assert "not queued" in failed.error
    finally:
        await output.close()


async def test_close_reopen_leaves_no_unconfirmed_future(monkeypatch: pytest.MonkeyPatch) -> None:
    producers: list[_BatchProducer] = []
    monkeypatch.setattr(
        aiokafka,
        "AIOKafkaProducer",
        lambda **options: producers.append(_BatchProducer()) or producers[-1],
    )
    output = _output()
    await output.open()
    try:
        key, stripe = _route("DEV-A")
        outcome = await output.write_encoded_batch(_encoded("DEV-A"), key=key, stripe=stripe)
        assert outcome.delivered == (0,)
        assert all(future.awaited == 1 for future in producers[0].futures)
    finally:
        await output.close()
    assert producers[0].stopped
    closed_key, closed_stripe = _route("DEV-A")
    closed = await output.write_encoded_batch(
        _encoded("DEV-A"), key=closed_key, stripe=closed_stripe
    )
    assert closed.delivered == () and "not connected" in closed.error
    await output.open()
    try:
        assert output._producer is not None  # noqa: SLF001
        key, stripe = _route("DEV-A")
        reopened = await output.write_encoded_batch(_encoded("DEV-A"), key=key, stripe=stripe)
        assert reopened.delivered == (0,)
        assert all(future.awaited == 1 for future in producers[1].futures)
    finally:
        await output.close()


async def test_write_encoded_batch_without_open_fails_closed() -> None:
    output = _output()
    key, stripe = _route("DEV-A")
    outcome = await output.write_encoded_batch(_encoded("DEV-A"), key=key, stripe=stripe)
    assert outcome.delivered == ()
    assert "not connected" in outcome.error


def test_partition_cache_is_an_ordered_dict() -> None:
    # The LRU structure used by write_encoded_batch remains an OrderedDict.
    output = _output()
    assert isinstance(output._partition_cache, OrderedDict)  # noqa: SLF001
