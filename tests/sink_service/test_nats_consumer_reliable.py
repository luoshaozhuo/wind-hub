"""真实 JetStream 下 NatsSinkConsumer 的 reliable 投递语义。

只依赖真实 NATS（``-js``），不接任何下行 Sink，专注消费者契约：

- EXPLICIT ack：处理成功后消息不重投；
- NAK 指数退避：写入临时失败按 attempts 退避重投，最终成功仅落一次；
- durable 隔离：两个 durable 各自独立收到全量消息；
- clear()：只跳过本 durable 积压，重建后 DeliverPolicy.NEW，旧消息不再投递。

任一断言失败即为 RELATED 生产缺陷。NATS 不可达时由 ``nats_service``
fixture 明确 skip，绝不以内存替身冒充。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import nats
import pytest

from contracts.sample_message import SampleMessage
from sink_service.infrastructure.nats_consumer import NatsSinkConsumer
from sink_service.nats_init import ensure_history_stream

pytestmark = pytest.mark.real_service

FARM = "test"


def _sample(point: str, *, seq: int) -> SampleMessage:
    now = datetime.now(UTC)
    return SampleMessage(
        wind_farm_id=FARM,
        collector_id="collector-r",
        task_id="t-reliable",
        device_id="dev-r",
        point_id=point,
        sample_id=f"collector-r:boot:r:{seq}",
        value=float(seq),
        quality="good",
        sample_time=now,
        publish_time=now,
        # reliable 信封必须省略 max_staleness_ms。
    )


async def _publish_hist(servers: str, point: str, seq: int) -> None:
    client = await nats.connect(servers=servers, connect_timeout=2.0)
    try:
        message = _sample(point, seq=seq)
        await client.jetstream().publish(
            message.subject("reliable"),
            message.encode(),
        )
    finally:
        await client.close()


def _consumer(
    servers: str,
    point: str,
    durable: str,
    handler: Callable[[SampleMessage], Awaitable[None]],
    *,
    ack_wait: float = 5.0,
) -> NatsSinkConsumer:
    # filter_subject 必须是合法 NATS subject（``>`` 只能结尾）；这里精确到点。
    exact_subject = f"wh.hist.{FARM}.collector-r.dev-r.{point}"
    return NatsSinkConsumer(
        [servers],
        exact_subject,
        durable=durable,
        handler=handler,
        ack_wait_seconds=ack_wait,
        max_ack_pending=1,
    )


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 12.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met before timeout")


async def test_reliable_message_acked_without_redelivery(
    nats_service: str,
) -> None:
    import asyncio

    await ensure_history_stream((nats_service,))
    received: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        received.append(sample)

    consumer = _consumer(nats_service, "p_ack", "durable_ack", handle, ack_wait=1.0)
    await consumer.open()
    try:
        await _publish_hist(nats_service, "p_ack", 1)
        await _wait_for(lambda: len(received) >= 1)
        # 超过 ack_wait 后仍只收到一次：成功处理已被 EXPLICIT ack 确认。
        await asyncio.sleep(1.6)
    finally:
        await consumer.close()
    assert len(received) == 1
    assert received[0].sample_id.endswith(":1")


async def test_failed_write_is_redelivered_with_backoff_then_acked(
    nats_service: str,
) -> None:
    await ensure_history_stream((nats_service,))
    attempts: list[int] = []
    delivered: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        # 前两次抛错触发 NAK 退避重投，第三次成功；同一样本最终只落一次。
        order = len(attempts) + 1
        attempts.append(order)
        if order < 3:
            raise RuntimeError("transient target outage")
        delivered.append(sample)

    consumer = _consumer(nats_service, "p_nak", "durable_nak", handle, ack_wait=1.0)
    await consumer.open()
    try:
        await _publish_hist(nats_service, "p_nak", 7)
        await _wait_for(lambda: len(delivered) >= 1, timeout=15.0)
    finally:
        await consumer.close()
    assert len(attempts) == 3, f"expected 2 redeliveries then success, got {attempts}"
    assert len(delivered) == 1
    assert delivered[0].value == 7.0


async def test_distinct_durables_each_receive_the_full_stream(
    nats_service: str,
) -> None:
    await ensure_history_stream((nats_service,))
    received_a: list[SampleMessage] = []
    received_b: list[SampleMessage] = []

    async def handle_a(sample: SampleMessage) -> None:
        received_a.append(sample)

    async def handle_b(sample: SampleMessage) -> None:
        received_b.append(sample)

    point = "p_iso"
    consumer_a = _consumer(nats_service, point, "durable_iso_a2", handle_a)
    consumer_b = _consumer(nats_service, point, "durable_iso_b2", handle_b)
    await consumer_a.open()
    await consumer_b.open()
    try:
        await _publish_hist(nats_service, point, 1)
        await _wait_for(lambda: len(received_a) >= 1 and len(received_b) >= 1)
    finally:
        await consumer_a.close()
        await consumer_b.close()

    # 两个 durable 各自独立收到全量消息（不共享队列、不抢占）。
    assert {m.value for m in received_a} == {1.0}
    assert {m.value for m in received_b} == {1.0}


async def test_clear_own_backlog_then_resume_consumption(nats_service: str) -> None:
    """KNOWN DEFECT（Round2，P1）：clear() 后 reliable Sink 无法恢复 push 消费。

    期望契约：clear() 跳过本 durable 积压后，同名 durable 以
    DeliverPolicy.NEW 重建，open() 能重新订阅且只收 clear 之后的新消息。

    现状（生产缺陷，勿改本测试去迁就）：clear() 用缺少 ``deliver_subject``
    的 ConsumerConfig 重建 durable，open() 的 jetstream.subscribe 走
    subscribe_bind 时抛 ``TypeError: config.deliver_subject is required``，
    导致 reliable Sink 在执行一次 Clear 后（含进程重启）永久无法恢复消费。
    """
    import pytest as _pytest  # noqa: PLC0415

    await ensure_history_stream((nats_service,))
    received: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        received.append(sample)

    point = "p_clear"
    consumer = _consumer(nats_service, point, "durable_clear2", handle)
    await consumer.open()
    try:
        await _publish_hist(nats_service, point, 1)
        await _wait_for(lambda: len(received) >= 1)
    finally:
        await consumer.close()

    # 停机期间再发一条，进入该 durable 的积压。
    await _publish_hist(nats_service, point, 2)

    stopped = _consumer(nats_service, point, "durable_clear2", handle)
    skipped = await stopped.clear()
    assert skipped >= 1, "clear() must report the durable backlog it skipped"

    resumed = _consumer(nats_service, point, "durable_clear2", handle)
    try:
        await resumed.open()
    except TypeError as exc:  # pragma: no cover - 缺陷修复后此行不再触发
        _pytest.fail(
            "P1: reliable Sink cannot resume after clear(); "
            f"jetstream subscribe failed: {exc}. "
            "clear() recreated the durable without deliver_subject."
        )
    try:
        await _publish_hist(nats_service, point, 3)
        await _wait_for(lambda: any(m.value == 3.0 for m in received))
    finally:
        await resumed.close()
    # 积压的 seq1/seq2 不再投递，仅消费 clear 之后的新消息。
    assert [m.value for m in received] == [1.0, 3.0]
