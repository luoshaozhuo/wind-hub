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

import aiokafka
import pytest

from sink_service.application.delivery_point import DeliveryPoint
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.adapters.outbound.realtime_adapters import KafkaOutput

pytestmark = [pytest.mark.unit, pytest.mark.reliability]

FARM = "farm-a"
COLLECTOR = "collector-1"
TOPIC = "qa-topic"
CACHE_CAP = 10000


def _sample(sample_id: str, *, device: str = "WT001") -> DeliveryPoint:
    now = datetime.now(UTC)
    return DeliveryPoint(
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id="task-1",
        device_id=device,
        point_id="active_power",
        sample_id=sample_id,
        value=1.0,
        quality="good",  # type: ignore[arg-type]
        sample_time=now,
        publish_time=now,
        receive_time=None,
        max_staleness_ms=5000,
    )


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
        self.client = type("Client", (), {"cluster": _FakeCluster()})()
        self.send_calls: list[tuple[str, bytes, int | None]] = []
        self.futures: list[_TrackingFuture] = []
        self.partition_calls = 0
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        if self.start_fails:
            raise RuntimeError("broker unreachable")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

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
        outcome = await output.write_batch(tuple(_sample(f"s{i}") for i in range(5)))
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.error == ""
        assert outcome.delivered == (0, 1, 2, 3, 4)
        # 首条 miss：send(partition=None) + 一次 _partition；其余 4 条显式分区。
        assert producer.partition_calls == 1
        assert [call[2] for call in producer.send_calls] == [None, 1, 1, 1, 1]
        assert dict(output._partition_cache) == {_key("WT001"): 1}  # noqa: SLF001

        # 第二批：全部缓存命中，partitioner 零调用。
        outcome2 = await output.write_batch(tuple(_sample(f"s{i}") for i in range(5, 8)))
        assert outcome2.delivered == (0, 1, 2)
        assert producer.partition_calls == 1
        assert all(call[2] == 1 for call in producer.send_calls[5:])
    finally:
        await output.close()


async def test_distinct_keys_get_independent_cached_partitions(
    producer_env: dict[str, Any],
) -> None:
    output = _output()
    await output.open()
    try:
        samples = tuple(_sample(f"s{i}", device=f"WT{i:03d}") for i in range(1, 5))
        outcome = await output.write_batch(samples)
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == (0, 1, 2, 3)
        assert producer.partition_calls == 4
        expected = {_key(f"WT{i:03d}"): i % 6 for i in range(1, 5)}
        assert dict(output._partition_cache) == expected  # noqa: SLF001
        # 再来一轮：零 partitioner 调用，分区与首轮一致。
        await output.write_batch(samples)
        assert producer.partition_calls == 4
        assert [call[2] for call in producer.send_calls[4:]] == [
            expected[_key(f"WT{i:03d}")] for i in range(1, 5)
        ]
    finally:
        await output.close()


async def test_cache_is_bounded_lru_and_evicts_least_recently_used(
    producer_env: dict[str, Any],
) -> None:
    output = _output()
    await output.open()
    try:
        # 填满 CACHE_CAP 个不同 Key。
        first_batch = tuple(
            _sample(f"fill-{i}", device=f"WT{i:03d}") for i in range(1, CACHE_CAP + 1)
        )
        await output.write_batch(first_batch)
        cache: OrderedDict[bytes, int] = output._partition_cache  # noqa: SLF001
        assert len(cache) == CACHE_CAP
        # 触碰最老 Key，使其变为最近使用，淘汰目标顺延到第二个 Key。
        oldest_key = _key("WT001")
        second_key = _key("WT002")
        newest_key = _key(f"WT{CACHE_CAP:03d}")
        await output.write_batch((_sample("touch", device="WT001"),))
        await output.write_batch((_sample("overflow", device=f"WT{CACHE_CAP + 1:03d}"),))
        assert len(cache) == CACHE_CAP
        assert oldest_key in cache  # 刚被触碰，保留
        assert second_key not in cache  # 最久未用，被淘汰
        assert newest_key in cache
        # 淘汰后重新写入该 Key：重新 miss 并经 partitioner 解析。
        producer: _FakeProducer = producer_env["producer"]
        calls_before = producer.partition_calls
        await output.write_batch((_sample("refill", device="WT002"),))
        assert producer.partition_calls == calls_before + 1
        assert second_key in cache
    finally:
        await output.close()


async def test_metadata_event_invalidates_cache(producer_env: dict[str, Any]) -> None:
    output = _output()
    await output.open()
    try:
        await output.write_batch(tuple(_sample(f"s{i}") for i in range(3)))
        producer: _FakeProducer = producer_env["producer"]
        assert producer.partition_calls == 1
        producer.client.cluster.fire_metadata_event()
        assert output._partition_cache == {}  # noqa: SLF001
        # 失效后下一条重新 miss，但分区映射不变（同 metadata 代）。
        await output.write_batch((_sample("after"),))
        assert producer.partition_calls == 2
        assert dict(output._partition_cache) == {_key("WT001"): 1}  # noqa: SLF001
        assert producer.send_calls[-1][2] is None
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
        outcome = await output.write_batch(tuple(_sample(f"s{i}") for i in range(3)))
        assert outcome.delivered == ()
        assert "not queued" in outcome.error
        # send 在缓存写入前抛出，缓存不得残留半截状态。
        assert output._partition_cache == {}  # noqa: SLF001
        producer: _FakeProducer = producer_env["producer"]
        # break 语义：首条失败后不得继续尝试后续样本。
        assert len(producer.send_calls) == 1
    finally:
        await output.close()


async def test_partition_resolution_failure_defers_to_broker_confirmation(
    producer_env: dict[str, Any],
) -> None:
    # v1_23（2f2ccf8f）新契约：分区缓存仅为加速，send 已入队的 Future 必须
    # 被持有到 Broker 终局。_partition 失败只清空缓存，不得把已确认写入误判
    # 失败（否则上层 NAK 重投会制造重复写），也不得孤儿化该 Future。
    producer_env["flags"]["partition_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await output.write_batch((_sample("s0"),))
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == (0,)
        assert outcome.error == ""
        assert output._partition_cache == {}  # noqa: SLF001
        assert producer.partition_calls == 1
        assert len(producer.futures) == 1 and producer.futures[0].awaited == 1
    finally:
        await output.close()


async def test_partition_resolution_failure_with_rejected_confirmation_is_not_delivered(
    producer_env: dict[str, Any],
) -> None:
    producer_env["flags"]["partition_fails"] = True
    producer_env["flags"]["confirm_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await output.write_batch((_sample("s0"),))
        producer: _FakeProducer = producer_env["producer"]
        # Broker 终局失败：样本不得进入 delivered，Future 仍被完整等待。
        assert outcome.delivered == ()
        assert "not confirmed" in outcome.error
        assert output._partition_cache == {}  # noqa: SLF001
        assert producer.partition_calls == 1
        assert len(producer.futures) == 1 and producer.futures[0].awaited == 1
    finally:
        await output.close()


async def test_partition_resolution_failure_never_orphans_submitted_futures(
    producer_env: dict[str, Any],
) -> None:
    # 多样本批次：每个样本 send 成功后先持有 Future 再做缓存维护，
    # _partition 持续失败时全部 Future 仍须被 await，缓存保持空。
    producer_env["flags"]["partition_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await output.write_batch(tuple(_sample(f"s{i}") for i in range(3)))
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == (0, 1, 2)
        assert output._partition_cache == {}  # noqa: SLF001
        assert producer.partition_calls == 3
        assert len(producer.futures) == 3
        assert all(future.awaited == 1 for future in producer.futures)
    finally:
        await output.close()


async def test_write_batch_without_open_returns_error_outcome() -> None:
    output = _output()
    outcome = await output.write_batch((_sample("s0"),))
    assert outcome.delivered == ()
    assert "not connected" in outcome.error
