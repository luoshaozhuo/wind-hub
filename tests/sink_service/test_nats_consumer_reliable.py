"""真实 JetStream / Core NATS 下 NatsSinkConsumer 的投递语义。

只依赖真实 NATS（``-js``），不接任何下行 Sink，专注消费者契约：

- EXPLICIT ack：处理成功后消息不重投；
- NAK 指数退避：写入临时失败按 attempts 退避重投，最终成功仅落一次；
- durable 隔离：两个 durable 各自独立收到全量消息，清 A 的积压不影响 B；
- clear()：只跳过本 durable 积压，重建后 DeliverPolicy.NEW，旧消息不再投递，
  重复 clear、新进程实例重绑同名 durable 均能正常恢复；
- realtime（Core NATS）与 reliable（JetStream）走各自 subject，互不影响；
- 异常路径：NATS 不可达 / consumer 管理失败必须抛出，不得假成功。

任一断言失败即为 RELATED 生产缺陷。NATS 不可达时由 ``nats_service``
fixture 明确 skip，绝不以内存替身冒充。

测试卫生：WH_HISTORY 是持久化 stream，跨运行保留消息；固定 subject 会让
DeliverPolicy.ALL 的首次订阅收到历史运行的旧消息。因此每个用例使用唯一
point（唯一 filter subject）与唯一 durable 名，并在开始前尽力删除同名
durable，保证可重复执行。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import nats
import pytest

from contracts.sample_message import SampleMessage
from sink_service.infrastructure.nats_consumer import NatsSinkConsumer
from sink_service.nats_init import ensure_history_stream

pytestmark = pytest.mark.real_service

FARM = "test"
COLLECTOR = "collector-r"
DEVICE = "dev-r"
TASK = "t-reliable"


def _sample(point: str, *, seq: int, max_staleness_ms: int | None = None) -> SampleMessage:
    now = datetime.now(UTC)
    return SampleMessage(
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id=TASK,
        device_id=DEVICE,
        point_id=point,
        sample_id=f"{COLLECTOR}:boot:r:{seq}:{point}",
        value=float(seq),
        quality="good",
        sample_time=now,
        publish_time=now,
        # reliable 信封必须省略 max_staleness_ms；realtime 必填。
        max_staleness_ms=max_staleness_ms,
    )


async def _publish(servers: str, point: str, seq: int, *, delivery: str) -> None:
    client = await nats.connect(servers=servers, connect_timeout=2.0)
    try:
        staleness = 5000 if delivery == "realtime" else None
        message = _sample(point, seq=seq, max_staleness_ms=staleness)
        subject = message.subject(delivery)  # type: ignore[arg-type]
        if delivery == "reliable":
            await client.jetstream().publish(subject, message.encode())
        else:
            # realtime 走 Core NATS（stream 只收 wh.hist.>）。
            await client.publish(subject, message.encode())
    finally:
        await client.close()


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
    stream: str = "WH_HISTORY",
) -> NatsSinkConsumer:
    # filter_subject 必须是合法 NATS subject（``>`` 只能结尾）；这里精确到点。
    exact_subject = f"wh.hist.{FARM}.{COLLECTOR}.{DEVICE}.{point}"
    return NatsSinkConsumer(
        [servers],
        exact_subject,
        durable=durable,
        handler=handler,
        ack_wait_seconds=ack_wait,
        max_ack_pending=1,
        stream=stream,
    )


async def _purge_durable(servers: str, durable: str, *, stream: str = "WH_HISTORY") -> None:
    """尽力删除历史运行残留的同名 durable（测试卫生，非被测路径）。"""
    client = await nats.connect(servers=servers, connect_timeout=2.0)
    try:
        await client.jetstream().delete_consumer(stream, durable)
    except Exception:
        # 不存在即干净状态。
        pass
    finally:
        await client.close()


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 12.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not met before timeout")


def _unique_point(prefix: str) -> tuple[str, str]:
    token = uuid4().hex[:10]
    return f"{prefix}_{token}", f"dur_{prefix}_{token}"


async def test_reliable_message_acked_without_redelivery(
    nats_service: str,
) -> None:
    import asyncio

    await ensure_history_stream((nats_service,))
    point, durable = _unique_point("p_ack")
    await _purge_durable(nats_service, durable)
    received: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        received.append(sample)

    consumer = _consumer(nats_service, point, durable, handle, ack_wait=1.0)
    await consumer.open()
    try:
        await _publish_hist(nats_service, point, 1)
        await _wait_for(lambda: len(received) >= 1)
        # 超过 ack_wait 后仍只收到一次：成功处理已被 EXPLICIT ack 确认。
        await asyncio.sleep(1.6)
    finally:
        await consumer.close()
    assert len(received) == 1
    assert received[0].sample_id.endswith(f":1:{point}")


async def test_failed_write_is_redelivered_with_backoff_then_acked(
    nats_service: str,
) -> None:
    await ensure_history_stream((nats_service,))
    point, durable = _unique_point("p_nak")
    await _purge_durable(nats_service, durable)
    attempts: list[int] = []
    delivered: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        # 前两次抛错触发 NAK 退避重投，第三次成功；同一样本最终只落一次。
        order = len(attempts) + 1
        attempts.append(order)
        if order < 3:
            raise RuntimeError("transient target outage")
        delivered.append(sample)

    consumer = _consumer(nats_service, point, durable, handle, ack_wait=1.0)
    await consumer.open()
    try:
        await _publish_hist(nats_service, point, 7)
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
    point, base = _unique_point("p_iso")
    durable_a = f"{base}_a"
    durable_b = f"{base}_b"
    await _purge_durable(nats_service, durable_a)
    await _purge_durable(nats_service, durable_b)
    received_a: list[SampleMessage] = []
    received_b: list[SampleMessage] = []

    async def handle_a(sample: SampleMessage) -> None:
        received_a.append(sample)

    async def handle_b(sample: SampleMessage) -> None:
        received_b.append(sample)

    consumer_a = _consumer(nats_service, point, durable_a, handle_a)
    consumer_b = _consumer(nats_service, point, durable_b, handle_b)
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
    """clear() 跳过本 durable 积压，重建后同名 durable 恢复 push 消费。

    契约：clear() 以 DeliverPolicy.NEW 重建 push durable（带服务端持久化的
    deliver_subject），open() 能重新订阅；clear 之前积压的旧消息不再投递，
    clear 之后发布的新消息正常投递且不重复。
    """
    await ensure_history_stream((nats_service,))
    point, durable = _unique_point("p_clear")
    await _purge_durable(nats_service, durable)
    received: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        received.append(sample)

    consumer = _consumer(nats_service, point, durable, handle)
    await consumer.open()
    try:
        await _publish_hist(nats_service, point, 1)
        await _wait_for(lambda: len(received) >= 1)
    finally:
        await consumer.close()

    # 停机期间再发一条，进入该 durable 的积压。
    await _publish_hist(nats_service, point, 2)

    stopped = _consumer(nats_service, point, durable, handle)
    skipped = await stopped.clear()
    assert skipped >= 1, "clear() must report the durable backlog it skipped"

    resumed = _consumer(nats_service, point, durable, handle)
    await resumed.open()
    try:
        await _publish_hist(nats_service, point, 3)
        await _wait_for(lambda: any(m.value == 3.0 for m in received))
    finally:
        await resumed.close()
    # 积压的 seq2 不再投递（seq1 clear 前已投递），仅消费 clear 之后的新消息。
    assert [m.value for m in received] == [1.0, 3.0]


async def test_clear_backlog_isolation_between_durables(nats_service: str) -> None:
    """清 A 的积压只影响 A：B 重绑后仍收到自己的积压。"""
    await ensure_history_stream((nats_service,))
    point, base = _unique_point("p_clriso")
    durable_a = f"{base}_a"
    durable_b = f"{base}_b"
    await _purge_durable(nats_service, durable_a)
    await _purge_durable(nats_service, durable_b)
    got_a: list[SampleMessage] = []
    got_b: list[SampleMessage] = []

    async def handle_a(sample: SampleMessage) -> None:
        got_a.append(sample)

    async def handle_b(sample: SampleMessage) -> None:
        got_b.append(sample)

    # 两个 durable 都先建立消费位点，再同时停机。
    a1 = _consumer(nats_service, point, durable_a, handle_a)
    b1 = _consumer(nats_service, point, durable_b, handle_b)
    await a1.open()
    await b1.open()
    await _publish_hist(nats_service, point, 1)
    await _wait_for(lambda: len(got_a) >= 1 and len(got_b) >= 1)
    await a1.close()
    await b1.close()

    # 双停期间积压一条（两个 durable 各自待投递）。
    await _publish_hist(nats_service, point, 2)

    # 只清 A 的积压；B 不动。
    a_stopped = _consumer(nats_service, point, durable_a, handle_a)
    assert await a_stopped.clear() >= 1

    a2 = _consumer(nats_service, point, durable_a, handle_a)
    b2 = _consumer(nats_service, point, durable_b, handle_b)
    await a2.open()
    await b2.open()
    try:
        await _publish_hist(nats_service, point, 3)
        await _wait_for(
            lambda: any(m.value == 3.0 for m in got_a) and any(m.value == 3.0 for m in got_b)
        )
    finally:
        await a2.close()
        await b2.close()

    assert [m.value for m in got_a] == [1.0, 3.0]  # seq2 积压被 A 自己跳过
    assert [m.value for m in got_b] == [1.0, 2.0, 3.0]  # B 不受影响


async def test_clear_then_new_process_instance_resumes_durable(
    nats_service: str,
) -> None:
    """Clear→恢复后，全新进程实例（全新连接）重绑同名 durable 继续消费。

    模拟 sink_service 进程重启：durable 与其投递位点由服务端持久化，
    新实例只 bind 不重建；既不重放已确认消息，也不漏新消息。
    """
    await ensure_history_stream((nats_service,))
    point, durable = _unique_point("p_restart")
    await _purge_durable(nats_service, durable)
    received: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        received.append(sample)

    first = _consumer(nats_service, point, durable, handle)
    await first.open()
    try:
        await _publish_hist(nats_service, point, 1)
        await _wait_for(lambda: any(m.value == 1.0 for m in received))
    finally:
        await first.close()

    await _publish_hist(nats_service, point, 2)
    await _consumer(nats_service, point, durable, handle).clear()

    # 实例 1（Clear 后的恢复实例）消费 clear 后的 seq3。
    instance_one = _consumer(nats_service, point, durable, handle)
    await instance_one.open()
    try:
        await _publish_hist(nats_service, point, 3)
        await _wait_for(lambda: any(m.value == 3.0 for m in received))
    finally:
        await instance_one.close()

    # 全新实例（模拟进程重启）：停机期间积压 seq4，重绑后只收 seq4。
    await _publish_hist(nats_service, point, 4)
    instance_two = _consumer(nats_service, point, durable, handle)
    await instance_two.open()
    try:
        await _wait_for(lambda: any(m.value == 4.0 for m in received))
    finally:
        await instance_two.close()

    assert [m.value for m in received] == [1.0, 3.0, 4.0]


async def test_repeated_clear_leaves_healthy_consumer(nats_service: str) -> None:
    """重复 Clear 不损坏配置：第二次 clear 报告 0 积压，恢复后仍收新消息。"""
    await ensure_history_stream((nats_service,))
    point, durable = _unique_point("p_2clear")
    await _purge_durable(nats_service, durable)
    received: list[SampleMessage] = []

    async def handle(sample: SampleMessage) -> None:
        received.append(sample)

    consumer = _consumer(nats_service, point, durable, handle)
    await consumer.open()
    try:
        await _publish_hist(nats_service, point, 1)
        await _wait_for(lambda: len(received) >= 1)
    finally:
        await consumer.close()

    stopped = _consumer(nats_service, point, durable, handle)
    assert await stopped.clear() == 0  # 无积压（seq1 已确认）
    assert await stopped.clear() == 0  # 再次 clear：幂等、不损坏

    resumed = _consumer(nats_service, point, durable, handle)
    await resumed.open()
    try:
        await _publish_hist(nats_service, point, 2)
        await _wait_for(lambda: any(m.value == 2.0 for m in received))
    finally:
        await resumed.close()

    assert [m.value for m in received] == [1.0, 2.0]


async def test_clear_requires_stopped_consumer(nats_service: str) -> None:
    """运行中 clear 必须拒绝（管理面不能在订阅存活时重建 durable）。"""
    await ensure_history_stream((nats_service,))
    point, durable = _unique_point("p_busy")
    await _purge_durable(nats_service, durable)

    async def handle(sample: SampleMessage) -> None:
        return None

    consumer = _consumer(nats_service, point, durable, handle)
    await consumer.open()
    try:
        with pytest.raises(RuntimeError, match="stop Sink before clearing"):
            await consumer.clear()
    finally:
        await consumer.close()


async def test_open_fails_fast_when_nats_unreachable() -> None:
    """NATS 不可达时 open() 必须抛出，不得静默成功。"""

    async def handle(sample: SampleMessage) -> None:
        return None

    consumer = NatsSinkConsumer(
        ["nats://127.0.0.1:9"],
        f"wh.hist.{FARM}.{COLLECTOR}.{DEVICE}.p_dead_{uuid4().hex[:8]}",
        durable=f"dur_dead_{uuid4().hex[:8]}",
        handler=handle,
    )
    with pytest.raises(Exception):  # noqa: PT011, B017 - 连接/超时类异常均可
        await consumer.open()
    assert not consumer.connected


async def test_clear_fails_when_stream_missing(nats_service: str) -> None:
    """consumer 管理操作失败（流不存在）时 clear() 必须抛出，不得返回假成功。"""
    await ensure_history_stream((nats_service,))
    point, durable = _unique_point("p_nostream")

    async def handle(sample: SampleMessage) -> None:
        return None

    consumer = _consumer(nats_service, point, durable, handle, stream="WH_HISTORY_MISSING")
    with pytest.raises(Exception):  # noqa: PT011, B017 - consumer_info 404/超时均可
        await consumer.clear()


async def test_realtime_core_subscription_independent_of_reliable(
    nats_service: str,
) -> None:
    """realtime（Core NATS）与 reliable（JetStream）subject 隔离、互不影响。"""
    await ensure_history_stream((nats_service,))
    token = uuid4().hex[:10]
    point = f"p_rt_{token}"
    rt_subject = f"wh.rt.{FARM}.{COLLECTOR}.{DEVICE}.{point}"
    hist_subject = f"wh.hist.{FARM}.{COLLECTOR}.{DEVICE}.{point}"
    durable = f"dur_rt_{token}"
    await _purge_durable(nats_service, durable)
    rt_received: list[SampleMessage] = []
    js_received: list[SampleMessage] = []

    async def handle_rt(sample: SampleMessage) -> None:
        rt_received.append(sample)

    async def handle_js(sample: SampleMessage) -> None:
        js_received.append(sample)

    realtime = NatsSinkConsumer([nats_service], rt_subject, durable=None, handler=handle_rt)
    reliable = NatsSinkConsumer([nats_service], hist_subject, durable=durable, handler=handle_js)
    await realtime.open()
    await reliable.open()
    try:
        await _publish(nats_service, point, 1, delivery="realtime")
        await _publish(nats_service, point, 2, delivery="reliable")
        await _wait_for(lambda: len(rt_received) >= 1 and len(js_received) >= 1)
    finally:
        await realtime.close()
        await reliable.close()

    assert [m.value for m in rt_received] == [1.0]
    assert [m.value for m in js_received] == [2.0]
