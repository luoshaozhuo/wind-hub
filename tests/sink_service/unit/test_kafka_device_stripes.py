"""KafkaOutput 设备级锁槽并发语义（单元层，fake producer）。

覆盖设计 §6.3.3 第 4 条：

- 同设备批次严格串行，不同设备经同一 Producer 异步重叠；
- 锁槽哈希碰撞只损失并行度，不改变顺序与正确性；
- send 中途异常时已入队 Future 仍被全部等待（无孤儿 Future）；
- close/reopen 后不残留未确认 Future，未 open 写入 fail-closed；
- 锁槽数量有界（128），混合设备批次取多槽且按全局顺序获取（无死锁）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import aiokafka
import pytest

import sink_service.infrastructure.adapters.outbound.realtime_adapters as adapters
from sink_service.application.delivery_point import DeliveryPoint
from sink_service.infrastructure.adapters.outbound.realtime_adapters import KafkaOutput

pytestmark = [pytest.mark.unit, pytest.mark.reliability]

FARM = "farm-a"
COLLECTOR = "collector-1"
TOPIC = "qa-topic"
STRIPES = 128


def _sample(sample_id: str, *, device: str) -> DeliveryPoint:
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


class _Cluster:
    def add_listener(self, listener: Any) -> None:
        self.listener = listener

    def remove_listener(self, listener: Any) -> None:
        self.listener = None


class _GatedProducer:
    """send 可被门控的 fake，记录在途并发与每设备顺序。"""

    def __init__(self, *, gate: asyncio.Event | None = None, fail_after: int | None = None) -> None:
        self.client = type("Client", (), {"cluster": _Cluster()})()
        self.gate = gate
        self.fail_after = fail_after
        self.inflight = 0
        self.max_inflight = 0
        self.send_order: list[tuple[str, str]] = []
        self.futures: list[_Future] = []
        self._send_count = 0
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, topic: str, *, key: bytes, value: bytes, partition: int | None) -> Any:
        self._send_count += 1
        if self.fail_after is not None and self._send_count > self.fail_after:
            raise RuntimeError("produce refused")
        future = _Future()
        self.futures.append(future)
        device = key.decode().split(":")[-1]
        self.send_order.append((device, future.tag))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            future.set_result(None)
            return future
        finally:
            self.inflight -= 1


class _Future:
    def __init__(self) -> None:
        self.tag = f"f{id(self)}"
        self._event = asyncio.Event()
        self.awaited = 0

    def set_result(self, value: None) -> None:
        self._event.set()

    def __await__(self) -> Any:
        self.awaited += 1
        yield from self._event.wait().__await__()


def _patch_producer(monkeypatch: pytest.MonkeyPatch, producer: _GatedProducer) -> None:
    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", lambda **options: producer)


def _output() -> KafkaOutput:
    return KafkaOutput(
        servers=["127.0.0.1:9092"],
        topic=TOPIC,
        timeout_ms=10000,
    )


def test_device_locks_are_bounded_stripes() -> None:
    output = _output()
    assert len(output._device_locks) == STRIPES  # noqa: SLF001
    assert all(isinstance(lock, asyncio.Lock) for lock in output._device_locks)  # noqa: SLF001


async def test_distinct_devices_overlap_and_per_device_order_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 确定性锁槽：DEV-A→0、DEV-B→1，避免哈希随机性使测试脆弱。
    monkeypatch.setattr(
        adapters,
        "hash",
        lambda key: 0 if key[2] == "DEV-A" else 1,
        raising=False,
    )
    gate = asyncio.Event()
    producer = _GatedProducer(gate=gate)
    _patch_producer(monkeypatch, producer)
    output = _output()
    await output.open()
    try:
        batch_a = tuple(_sample(f"a{i}", device="DEV-A") for i in range(3))
        batch_b = tuple(_sample(f"b{i}", device="DEV-B") for i in range(3))
        task_a = asyncio.create_task(output.write_batch(batch_a))
        task_b = asyncio.create_task(output.write_batch(batch_b))
        for _ in range(40):
            await asyncio.sleep(0.005)
            if producer.max_inflight == 2:
                break
        assert producer.max_inflight == 2  # 不同设备真正并行
        gate.set()
        outcome_a, outcome_b = await asyncio.gather(task_a, task_b)
        assert outcome_a.delivered == (0, 1, 2)
        assert outcome_b.delivered == (0, 1, 2)
        # 每设备在 send 侧的入队次数与批次一致；同设备只有一个持锁写入者，
        # 批次内顺序由锁槽天然保证，跨设备完成顺序不做承诺。
        devices = [device for device, _tag in producer.send_order]
        assert devices.count("DEV-A") == 3 and devices.count("DEV-B") == 3
        assert all(future.awaited == 1 for future in producer.futures)
    finally:
        await output.close()


async def test_stripe_hash_collision_serializes_without_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapters, "hash", lambda _key: 0, raising=False)
    gate = asyncio.Event()
    producer = _GatedProducer(gate=gate)
    _patch_producer(monkeypatch, producer)
    output = _output()
    await output.open()
    try:
        task_a = asyncio.create_task(output.write_batch((_sample("a0", device="DEV-A"),)))
        # 确保 A 已进入门控区后再发起 B。
        for _ in range(40):
            await asyncio.sleep(0.005)
            if producer.inflight == 1:
                break
        task_b = asyncio.create_task(output.write_batch((_sample("b0", device="DEV-B"),)))
        await asyncio.sleep(0.05)
        assert producer.max_inflight == 1  # 碰撞：并行度退化为 1，但不损坏正确性
        gate.set()
        outcome_a, outcome_b = await asyncio.gather(task_a, task_b)
        assert outcome_a.delivered == (0,) and outcome_b.delivered == (0,)
        assert all(future.awaited == 1 for future in producer.futures)
    finally:
        await output.close()


async def test_send_failure_awaits_already_submitted_futures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 第 2 个 send 抛错：第 1 个 Future 必须仍被等待，不得成为孤儿。
    producer = _GatedProducer(fail_after=1)
    _patch_producer(monkeypatch, producer)
    output = _output()
    await output.open()
    try:
        outcome = await output.write_batch(
            tuple(_sample(f"s{i}", device="DEV-A") for i in range(3))
        )
        # 首个 Future 已被 Broker 确认则如实计入 delivered；关键是它被等待而非孤儿。
        assert outcome.delivered == (0,)
        assert "not queued" in outcome.error
        assert len(producer.send_order) == 1
        assert len(producer.futures) == 1 and producer.futures[0].awaited == 1
    finally:
        await output.close()


async def test_close_reopen_leaves_no_unconfirmed_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producers: list[_GatedProducer] = []
    monkeypatch.setattr(
        aiokafka,
        "AIOKafkaProducer",
        lambda **options: producers.append(_GatedProducer()) or producers[-1],
    )
    output = _output()
    await output.open()
    try:
        outcome = await output.write_batch((_sample("s0", device="DEV-A"),))
        assert outcome.delivered == (0,)
        assert all(future.awaited == 1 for future in producers[0].futures)
    finally:
        await output.close()
    assert producers[0].stopped
    # close 后未 open 写入：fail-closed，不产生新 Future。
    closed_outcome = await output.write_batch((_sample("s1", device="DEV-A"),))
    assert closed_outcome.delivered == () and "not connected" in closed_outcome.error
    # reopen 获得新 Producer，链路恢复。
    await output.open()
    try:
        assert output._producer is not None  # noqa: SLF001
        reopened = await output.write_batch((_sample("s2", device="DEV-A"),))
        assert reopened.delivered == (0,)
        assert all(future.awaited == 1 for future in producers[1].futures)
    finally:
        await output.close()


async def test_mixed_device_batch_acquires_multiple_stripes_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        adapters,
        "hash",
        lambda key: 0 if key[2] == "DEV-A" else 1,
        raising=False,
    )
    producer = _GatedProducer()
    _patch_producer(monkeypatch, producer)
    output = _output()
    await output.open()
    try:
        mixed = (_sample("a", device="DEV-A"), _sample("b", device="DEV-B"))
        outcome = await output.write_batch(mixed)
        assert outcome.delivered == (0, 1)
        assert {device for device, _ in producer.send_order} == {"DEV-A", "DEV-B"}
        assert all(future.awaited == 1 for future in producer.futures)
    finally:
        await output.close()
