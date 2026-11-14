"""KafkaOutput.write_encoded_batch 进程内单测（fake producer，B2 BatchBuilder 表面）。

旧接口 write_batch 的等价覆盖在 test_kafka_output_partition_cache.py；
本文件钉死 write_encoded_batch 不得退化的契约（R10 B2 后由逐记录 send
迁移到 create_batch/send_batch，fake 表面随之迁移，业务断言一条不放松）：

- 预编码 bytes 原样上线，key/stripe 由调用方显式指定，首条 miss 走
  metadata 等待 + partitioner，同 Key 后续批次全部显式分区短路；
- 分区解析失败 fail-closed：任何 Builder 提交前即返回，无 Future 可孤儿；
- metadata 失效（R7 竞态）不清空在途 Future 的所有权、不抛 KeyError；
- send_batch 失败 fail-closed break，确认拒绝 UNKNOWN 不当成功；未 open 报错；
- stripe 映射到 128 把设备锁：同 stripe 串行、异 stripe 可并行。

R8 八项守卫的完整正式化在 tests/src/unit/test_b2_batch_builder.py，
本文件保留 B1 时代的历史测试名与业务意图，二者共享同一套 fake 基建。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sink_service.infrastructure.adapters.outbound.realtime_adapters import KafkaOutput
from tests.src.unit.test_b2_batch_builder import (
    KEY,
    STRIPE,
    TOPIC,
    _FakeProducer,
    _output,
    _payloads,
    build_producer_env,
)

pytestmark = [pytest.mark.unit, pytest.mark.reliability]


@pytest.fixture
def producer_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return build_producer_env(monkeypatch)


async def test_encoded_bytes_go_on_wire_unchanged_with_explicit_key_and_stripe(
    producer_env: dict[str, Any],
) -> None:
    output = _output()
    await output.open()
    try:
        payloads = _payloads(5)
        outcome = await output.write_encoded_batch(payloads, key=KEY, stripe=STRIPE)
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.error == ""
        assert outcome.delivered == (0, 1, 2, 3, 4)
        # 首条 miss：一次 metadata 等待 + 一次 partitioner，之后缓存短路。
        assert producer.metadata_waits == 1
        assert producer.partition_calls == 1
        assert len(producer.submissions) == 1
        builder, topic, partition = producer.submissions[0]
        assert topic == TOPIC and partition == 3
        # key 与预编码 bytes 原样透传，生产层不重新序列化。
        assert [record[0] for record in builder.records] == [KEY] * 5
        assert [record[1] for record in builder.records] == list(payloads)
        assert dict(output._partition_cache) == {KEY: 3}  # noqa: SLF001

        # 第二批：缓存全命中，metadata/partitioner 零新增调用。
        outcome2 = await output.write_encoded_batch(_payloads(2), key=KEY, stripe=STRIPE)
        assert outcome2.delivered == (0, 1)
        assert producer.metadata_waits == 1
        assert producer.partition_calls == 1
        assert all(partition == 3 for _builder, _topic, partition in producer.submissions)
    finally:
        await output.close()


async def test_partition_failure_never_orphans_submitted_encoded_futures(
    producer_env: dict[str, Any],
) -> None:
    producer_env["flags"]["partition_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await output.write_encoded_batch(_payloads(4), key=KEY, stripe=STRIPE)
        producer: _FakeProducer = producer_env["producer"]
        # B2 语义：分区解析先于任何 Builder 提交，失败即整体 fail-closed，
        # 没有任何已提交 Future——无孤儿的业务断言以「零提交零 Future」钉死。
        assert outcome.delivered == ()
        assert "partition unavailable" in outcome.error
        assert output._partition_cache == {}  # noqa: SLF001
        assert producer.submissions == []
        assert producer.futures == []
    finally:
        await output.close()


async def test_metadata_invalidation_during_encoded_send_does_not_raise_or_orphan(
    producer_env: dict[str, Any],
) -> None:
    """R7 竞态回归：Builder 在途确认期间 metadata listener 清空缓存。

    旧缺陷在缓存命中分支无条件 move_to_end，缓存被清空后 KeyError 使
    已提交 Future 孤儿化并触发 NAK 重投前缀重复。现行实现用
    `elif record_key in cache` 守卫，本用例钉死该行为。
    """
    output = _output()
    await output.open()
    producer: _FakeProducer = producer_env["producer"]
    try:
        # 第一批建立缓存。
        await output.write_encoded_batch(_payloads(1), key=KEY, stripe=STRIPE)
        assert dict(output._partition_cache) == {KEY: 3}  # noqa: SLF001

        def invalidate() -> None:
            producer.client.cluster.fire_metadata_event()
            # 只在第二批提交期间触发一次，随后摘除钩子避免清空重建后的缓存
            producer_env["gates"].pop("on_send_batch", None)

        producer_env["gates"]["on_send_batch"] = invalidate
        outcome = await output.write_encoded_batch(_payloads(3), key=KEY, stripe=STRIPE)

        assert outcome.delivered == (0, 1, 2)
        assert outcome.error == ""
        assert output._partition_cache == {}  # noqa: SLF001
        assert all(future.awaited == 1 for future in producer.futures)
        # 下一批重新走 metadata 等待并重建缓存，全程不抛 KeyError。
        await output.write_encoded_batch(_payloads(1), key=KEY, stripe=STRIPE)
        assert dict(output._partition_cache) == {KEY: 3}  # noqa: SLF001
        assert producer.metadata_waits == 2
    finally:
        await output.close()


async def test_send_failure_fails_closed_and_keeps_cache_empty(
    producer_env: dict[str, Any],
) -> None:
    producer_env["flags"]["send_batch_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await output.write_encoded_batch(_payloads(3), key=KEY, stripe=STRIPE)
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == ()
        assert "not queued" in outcome.error
        assert output._partition_cache == {}  # noqa: SLF001
        # break：首个 Builder 被拒后不再尝试后续提交
        assert producer.send_attempts == 1
        assert producer.submissions == []
    finally:
        await output.close()


async def test_rejected_confirmation_is_not_delivered_but_future_awaited(
    producer_env: dict[str, Any],
) -> None:
    producer_env["flags"]["confirm_fails"] = True
    output = _output()
    await output.open()
    try:
        outcome = await output.write_encoded_batch(_payloads(2), key=KEY, stripe=STRIPE)
        producer: _FakeProducer = producer_env["producer"]
        assert outcome.delivered == ()
        assert "not confirmed" in outcome.error
        # B2 下两点同属一个 Builder，唯一 Future 仍被 await 到终局
        assert len(producer.futures) == 1
        assert all(future.awaited == 1 for future in producer.futures)
    finally:
        await output.close()


async def test_encoded_write_without_open_returns_error_outcome() -> None:
    output = KafkaOutput(servers=["127.0.0.1:9092"], topic=TOPIC, timeout_ms=10000)
    outcome = await output.write_encoded_batch(_payloads(1), key=KEY, stripe=STRIPE)
    assert outcome.delivered == ()
    assert "not connected" in outcome.error


async def test_same_stripe_serializes_while_distinct_stripes_overlap(
    producer_env: dict[str, Any],
) -> None:
    output = _output()
    await output.open()
    producer: _FakeProducer = producer_env["producer"]
    gate = asyncio.Event()
    producer_env["gates"]["send_gate"] = gate
    try:
        other_stripe = (STRIPE + 1) % 128
        first = asyncio.create_task(
            output.write_encoded_batch(_payloads(1), key=KEY, stripe=STRIPE)
        )
        # 等第一条进入门控 send_batch。
        for _ in range(40):
            await asyncio.sleep(0.005)
            if producer.send_attempts:
                break
        # 同 stripe 必须阻塞在设备锁上，不能进入 send_batch。
        same_stripe = asyncio.create_task(
            output.write_encoded_batch(_payloads(1), key=KEY, stripe=STRIPE)
        )
        other = asyncio.create_task(
            output.write_encoded_batch(
                _payloads(1), key=b"farm-a:collector-1:WT002", stripe=other_stripe
            )
        )
        for _ in range(40):
            await asyncio.sleep(0.005)
            if producer.send_attempts == 2:
                break
        # 异 stripe 已并行进入，同 stripe 仍被锁挡住；门控未放行无完成提交
        assert producer.send_attempts == 2
        assert len(producer.submissions) == 0
        gate.set()
        results = await asyncio.gather(first, same_stripe, other)
        assert all(result.delivered == (0,) for result in results)
        assert len(producer.submissions) == 3
    finally:
        await output.close()
