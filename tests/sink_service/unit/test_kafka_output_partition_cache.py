"""KafkaOutput L1 分区缓存与 L3 producer 调参的进程内单测（fake producer）。

覆盖第六轮生产化复测要求的无 Docker 维度：

- 构造参数边界：linger_ms 只接受整数 0；max_batch_size 只接受正整数；
  布尔值、浮点、字符串、负数、None 全部 fail-closed；
- open() 向 aiokafka 显式传递 linger_ms=0 与 max_batch_size，metadata
  listener 随 open/close 精确注册/移除，缓存随 close 清空；
- 同 Key 仅首条走默认 partitioner（send partition=None + 一次 _partition），
  后续全部显式分区短路；不同 Key 独立缓存；LRU 上限 10000 淘汰最久未用；
- metadata 事件触发缓存失效，失效后下一条重新走 partitioner；
- send 异常、_partition 异常、未 open 写入、open 启动失败均不产生
  "UNKNOWN 当成功"，且资源路径闭合。

fake 只替换下游 producer 端口，被测 KafkaOutput 为生产原类。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from zlib import crc32

import aiokafka
import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.adapters.outbound.kafka_wire import (
    encode_kafka_points,
)
from sink_service.infrastructure.adapters.outbound.realtime_adapters import KafkaOutput

pytestmark = [pytest.mark.unit, pytest.mark.reliability]

FARM = "farm-a"
COLLECTOR = "collector-1"
TOPIC = "qa-topic"
CACHE_CAP = 10000


class _FakeCluster:
    def __init__(self) -> None:
        self.listeners: list[Any] = []

    def add_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: Any) -> None:
        self.listeners.remove(listener)

    def fire_metadata_event(self) -> None:
        for listener in list(self.listeners):
            listener(self)


class _FakeClient:
    def __init__(self, cluster: _FakeCluster) -> None:
        self.cluster = cluster

    async def _wait_on_metadata(self, topic: str) -> None:
        return None


class _BatchBuilder:
    def __init__(self) -> None:
        self.records: list[tuple[bytes, bytes]] = []

    def append(self, *, key: bytes, value: bytes, timestamp: Any) -> object:
        self.records.append((key, value))
        return object()

    def close(self) -> None:
        return None


class _TrackingFuture:
    """记录是否被 await 的可等待对象，用于证明没有 Future 被孤儿化。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.awaited = 0
        self._fail = fail

    def __await__(self) -> Any:
        self.awaited += 1
        if self._fail:
            raise RuntimeError("broker rejected confirmation")
        yield


class _FakeProducer:
    """记录 send/_partition 调用；分区由固定 key->partition 映射决定。"""

    def __init__(
        self,
        options: dict[str, Any],
        partition_map: dict[bytes, int],
        *,
        send_fails: bool = False,
        partition_fails: bool = False,
        start_fails: bool = False,
        confirm_fails: bool = False,
    ) -> None:
        self.options = options
        self._map = partition_map
        self.send_fails = send_fails
        self.partition_fails = partition_fails
        self.start_fails = start_fails
        self.confirm_fails = confirm_fails
        self.client = _FakeClient(_FakeCluster())
        self.send_calls: list[tuple[str, bytes, int | None]] = []
        self.futures: list[_TrackingFuture] = []
        self.batch_calls: list[tuple[str, int | None]] = []
        self.partition_calls = 0
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        if self.start_fails:
            raise RuntimeError("broker unreachable")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def create_batch(self) -> _BatchBuilder:
        return _BatchBuilder()

    async def send_batch(
        self, builder: _BatchBuilder, topic: str, partition: int | None
    ) -> _TrackingFuture:
        if self.send_fails:
            raise RuntimeError("produce refused")
        for key, _value in builder.records:
            self.send_calls.append((topic, key, partition))
        self.batch_calls.append((topic, partition))
        future = _TrackingFuture(fail=self.confirm_fails)
        self.futures.append(future)
        return future

    async def send(self, topic: str, *, key: bytes, value: bytes, partition: int | None) -> Any:
        self.send_calls.append((topic, key, partition))
        if self.send_fails:
            raise RuntimeError("produce refused")
        future = _TrackingFuture(fail=self.confirm_fails)
        self.futures.append(future)
        return future

    def _partition(
        self,
        topic: str,
        partition: int | None,
        key: bytes,
        value: Any,
        serialized_key: bytes,
        serialized_value: Any,
    ) -> int:
        self.partition_calls += 1
        if self.partition_fails:
            raise RuntimeError("metadata unavailable")
        return self._map[serialized_key]


def _key(device: str) -> bytes:
    return ":".join((FARM, COLLECTOR, device)).encode("utf-8")


def _stripe(device: str) -> int:
    return crc32(_key(device)) % 128


def _encoded(device: str, sample_ids: list[str]) -> tuple[bytes, ...]:
    now = datetime.now(UTC)
    batch = SampleBatch(
        batch_id="b-cache",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id="task-1",
        device_type="wind_turbine",
        device_id=device,
        point_table_id="point_table_wtg_sim_v1.0.0",
        publish_time=now,
        samples=tuple(
            SampleItem(
                point_id=f"p{index:03d}",
                sample_id=sample_id,
                value=1.0,
                quality="good",
                sample_time=now,
                max_staleness_ms=None,
            )
            for index, sample_id in enumerate(sample_ids)
        ),
    )
    return encode_kafka_points(batch, batch.samples, data_level="protocol")


async def _write(output: KafkaOutput, device: str, sample_ids: list[str]):
    return await output.write_encoded_batch(
        _encoded(device, sample_ids), key=_key(device), stripe=_stripe(device)
    )


@pytest.fixture
def producer_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    holders: dict[str, _FakeProducer] = {}
    partition_map = {_key(f"WT{i:03d}"): i % 6 for i in range(1, CACHE_CAP + 2)}
    flags: dict[str, bool] = {}

    def factory(**options: Any) -> _FakeProducer:
        producer = _FakeProducer(
            options,
            partition_map,
            send_fails=flags.get("send_fails", False),
            partition_fails=flags.get("partition_fails", False),
            start_fails=flags.get("start_fails", False),
            confirm_fails=flags.get("confirm_fails", False),
        )
        holders["producer"] = producer
        return producer

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", factory)
    holders["flags"] = flags
    return holders


def _output(*, linger_ms: int = 0, max_batch_size: int = 131072) -> KafkaOutput:
    return KafkaOutput(
        servers=["127.0.0.1:9092"],
        topic=TOPIC,
        timeout_ms=10000,
        linger_ms=linger_ms,
        max_batch_size=max_batch_size,
    )


# ---- 构造参数边界 -------------------------------------------------------


@pytest.mark.parametrize("bad_linger", [1, -1, 10, True, False, 0.0, "0", None])
def test_linger_ms_rejects_anything_but_integer_zero(bad_linger: Any) -> None:
    with pytest.raises(ConfigurationError, match="linger_ms must be 0"):
        KafkaOutput(
            servers=["127.0.0.1:9092"],
            topic=TOPIC,
            timeout_ms=10000,
            linger_ms=bad_linger,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad_batch", [0, -1, True, False, 1.5, "16384", None])
def test_max_batch_size_rejects_non_positive_or_non_integer(bad_batch: Any) -> None:
    with pytest.raises(ConfigurationError, match="max_batch_size must be positive"):
        KafkaOutput(
            servers=["127.0.0.1:9092"],
            topic=TOPIC,
            timeout_ms=10000,
            max_batch_size=bad_batch,  # type: ignore[arg-type]
        )


def test_defaults_and_explicit_valid_values_accepted() -> None:
    output = _output()
    assert output._max_batch_size == 131072  # noqa: SLF001
    tuned = _output(max_batch_size=16384)
    assert tuned._max_batch_size == 16384  # noqa: SLF001


# ---- open/close 资源与参数透传 ------------------------------------------


async def test_open_propagates_l3_options_and_manages_listener(
    producer_env: dict[str, Any],
) -> None:
    output = _output(max_batch_size=65536)
    await output.open()
    producer: _FakeProducer = producer_env["producer"]
    try:
        assert producer.started and not producer.stopped
        assert producer.options["linger_ms"] == 0
        assert producer.options["max_batch_size"] == 65536
        assert producer.options["acks"] == "all"
        assert producer.options["enable_idempotence"] is True
        assert len(producer.client.cluster.listeners) == 1
        # 幂等 open：已连接时直接返回，不重复注册 listener。
        await output.open()
        assert len(producer.client.cluster.listeners) == 1
    finally:
        await output.close()
    assert producer.stopped
    assert producer.client.cluster.listeners == []
    assert output._partition_cache == {}  # noqa: SLF001
    assert output._producer is None  # noqa: SLF001


async def test_open_failure_stops_producer_and_does_not_register_listener(
    producer_env: dict[str, Any],
) -> None:
    producer_env["flags"]["start_fails"] = True
    output = _output()
    with pytest.raises(RuntimeError, match="broker unreachable"):
        await output.open()
    producer: _FakeProducer = producer_env["producer"]
    assert producer.stopped
    assert producer.client.cluster.listeners == []
    assert output._producer is None  # noqa: SLF001


# ---- L1 缓存行为 ---------------------------------------------------------


async def test_same_key_partitions_once_then_uses_explicit_partition(
    producer_env: dict[str, Any],
) -> None:
    output = _output()
    await output.open()
    try:
        outcome = await _write(output, "WT001", [f"s{i}" for i in range(5)])
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.error == ""
        assert outcome.delivered == (0, 1, 2, 3, 4)
        # One key resolves its partition exactly once; the whole batch is sent
        # under that partition via a single send_batch.
        assert producer.partition_calls == 1
        assert len(producer.batch_calls) == 1
        assert [call[2] for call in producer.send_calls] == [1, 1, 1, 1, 1]
        assert dict(output._partition_cache) == {_key("WT001"): 1}  # noqa: SLF001

        # Second batch: cache hit, the partitioner is not invoked again.
        outcome2 = await _write(output, "WT001", [f"s{i}" for i in range(5, 8)])
        assert outcome2.delivered == (0, 1, 2)
        assert producer.partition_calls == 1
        assert len(producer.batch_calls) == 2
        assert all(call[2] == 1 for call in producer.send_calls[5:])
    finally:
        await output.close()


async def test_distinct_keys_get_independent_cached_partitions(
    producer_env: dict[str, Any],
) -> None:
    output = _output()
    await output.open()
    try:
        devices = [f"WT{i:03d}" for i in range(1, 5)]
        # Each device key is a separate pre-encoded batch in the durable path.
        outcomes = [await _write(output, device, [f"s{i}"]) for i, device in enumerate(devices, 1)]
        producer: _FakeProducer = producer_env["producer"]
        assert all(outcome.delivered == (0,) for outcome in outcomes)
        assert producer.partition_calls == 4
        expected = {_key(device): i % 6 for i, device in enumerate(devices, 1)}
        assert dict(output._partition_cache) == expected  # noqa: SLF001
        # Second round: zero additional partitioner calls, same partitions.
        before = producer.partition_calls
        for i, device in enumerate(devices, 1):
            await _write(output, device, [f"s{i}-again"])
        assert producer.partition_calls == before
        assert [call[2] for call in producer.send_calls[4:]] == [
            expected[_key(device)] for device in devices
        ]
    finally:
        await output.close()


async def test_cache_is_bounded_lru_and_evicts_least_recently_used(
    producer_env: dict[str, Any],
) -> None:
    output = _output()
    await output.open()
    try:
        # Fill CACHE_CAP distinct device keys, one pre-encoded batch each.
        for index in range(1, CACHE_CAP + 1):
            await _write(output, f"WT{index:03d}", [f"fill-{index}"])
        cache: OrderedDict[bytes, int] = output._partition_cache  # noqa: SLF001
        assert len(cache) == CACHE_CAP
        # Touch the oldest key so eviction moves to the second-oldest.
        oldest_key = _key("WT001")
        second_key = _key("WT002")
        newest_key = _key(f"WT{CACHE_CAP:03d}")
        await _write(output, "WT001", ["touch"])
        await _write(output, f"WT{CACHE_CAP + 1:03d}", ["overflow"])
        assert len(cache) == CACHE_CAP
        assert oldest_key in cache  # just touched, retained
        assert second_key not in cache  # least-recently-used, evicted
        assert newest_key in cache
        # Re-writing the evicted key misses again and goes through the partitioner.
        producer: _FakeProducer = producer_env["producer"]
        calls_before = producer.partition_calls
        await _write(output, "WT002", ["refill"])
        assert producer.partition_calls == calls_before + 1
        assert second_key in cache
    finally:
        await output.close()


async def test_metadata_event_invalidates_cache(producer_env: dict[str, Any]) -> None:
    output = _output()
    await output.open()
    try:
        await _write(output, "WT001", [f"s{i}" for i in range(3)])
        producer: _FakeProducer = producer_env["producer"]
        assert producer.partition_calls == 1
        producer.client.cluster.fire_metadata_event()
        assert output._partition_cache == {}  # noqa: SLF001
        # After invalidation the next write misses again but resolves the same
        # partition for the same metadata generation.
        await _write(output, "WT001", ["after"])
        assert producer.partition_calls == 2
        assert dict(output._partition_cache) == {_key("WT001"): 1}  # noqa: SLF001
        assert producer.send_calls[-1][2] == 1
    finally:
        await output.close()


# ---- 异常路径：禁止 UNKNOWN 当成功 --------------------------------------


async def test_send_failure_marks_batch_errored_and_keeps_cache_empty(
    producer_env: dict[str, Any],
) -> None:
    producer_env["flags"]["send_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await _write(output, "WT001", [f"s{i}" for i in range(3)])
        assert outcome.delivered == ()
        assert "not queued" in outcome.error
        # The failure rolls the partition cache back; no half state remains.
        assert output._partition_cache == {}  # noqa: SLF001
        producer: _FakeProducer = producer_env["producer"]
        # send_batch raised before enqueuing any record.
        assert len(producer.batch_calls) == 0
        assert len(producer.send_calls) == 0
    finally:
        await output.close()


async def test_partition_resolution_failure_blocks_enqueue_without_fabricating_success(
    producer_env: dict[str, Any],
) -> None:
    # Client-side partition resolution happens before send_batch in the durable
    # path: a resolution failure must clear the cache and enqueue nothing.
    producer_env["flags"]["partition_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await _write(output, "WT001", ["s0"])
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == ()
        assert "partition unavailable" in outcome.error
        assert output._partition_cache == {}  # noqa: SLF001
        assert producer.partition_calls == 1
        assert len(producer.batch_calls) == 0
        assert producer.futures == []
    finally:
        await output.close()


async def test_rejected_broker_confirmation_is_not_delivered_and_future_awaited(
    producer_env: dict[str, Any],
) -> None:
    # Partition resolves, but the broker confirmation future fails: the record
    # is never delivered and the future is still fully awaited (no orphan).
    producer_env["flags"]["confirm_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await _write(output, "WT001", ["s0"])
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == ()
        assert "not confirmed" in outcome.error
        assert producer.partition_calls == 1
        assert len(producer.futures) == 1 and producer.futures[0].awaited == 1
    finally:
        await output.close()


async def test_multi_record_batch_keeps_all_confirmations_under_one_partition(
    producer_env: dict[str, Any],
) -> None:
    # All records in one device batch share the resolved partition; a single
    # send_batch confirmation settles every index.
    output = _output()
    await output.open()
    try:
        outcome = await _write(output, "WT001", [f"s{i}" for i in range(3)])
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == (0, 1, 2)
        assert producer.partition_calls == 1
        assert len(producer.batch_calls) == 1
        assert len(producer.futures) == 1 and producer.futures[0].awaited == 1
    finally:
        await output.close()


async def test_write_encoded_batch_without_open_returns_error_outcome() -> None:
    output = _output()
    key, stripe = _key("WT001"), _stripe("WT001")
    outcome = await output.write_encoded_batch(_encoded("WT001", ["s0"]), key=key, stripe=stripe)
    assert outcome.delivered == ()
    assert "not connected" in outcome.error
