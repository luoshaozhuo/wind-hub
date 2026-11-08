"""Collector → SampleBatch v2 → NATS/JetStream → Sink Consumer 真实链路集成。

与组件测试的本质区别：NATS 以真实 ``nats:latest -js`` 容器运行，PubAck、
持久 Stream、Durable Consumer、DLQ、重投均为 broker 真实行为，无任何
NATS 内存替身。Docker 不可用时整个模块 skip（报告记 NOT_RUN），绝不以
Mock 结果冒充真实链路。

覆盖 Contracts v1_6 §5.3：
- 一个源采集事件 = 一条独立 NATS 批次，不合并周期；
- Realtime core-NATS 与 Reliable JetStream 通道隔离；
- Reliable PubAck 与 Sink 交付确认分离（broker 去重 vs 下游 ACK）；
- 不可解码消息进入独立 DLQ Stream 后才 ACK；
- Nats-Msg-Id 去重保证重复发布不产生重复投递。
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import nats
import pytest

from collector.infrastructure.publisher import CollectorPublisher
from contracts.sample_batch import SampleBatch
from core.application import ProtocolSample
from sink_service.infrastructure.nats_consumer import NatsSinkConsumer
from sink_service.nats_init import ensure_dlq_stream, ensure_history_stream
from tests.support.process import free_port
from tests.support.wait import wait_tcp_port

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.end_to_end,
]


@pytest.fixture(scope="module")
def nats_url() -> Iterator[str]:
    try:
        subprocess.run(["docker", "version"], capture_output=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"SKIPPED: docker unavailable for real NATS link: {exc}")
    port = free_port()
    name = f"batch-v2-js-{uuid.uuid4().hex[:10]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:4222",
                "nats:latest",
                "-js",
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        asyncio.run(wait_tcp_port("127.0.0.1", port, timeout=15.0))
        yield f"nats://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def _sample(point: str) -> ProtocolSample:
    return ProtocolSample(point_id=point, value=1, timestamp=datetime.now(UTC))


async def _drain_event(events: list[SampleBatch], target: int, timeout: float = 10.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while len(events) < target and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert len(events) == target, f"expected {target} batches, got {len(events)}"


@pytest.mark.asyncio
async def test_realtime_each_source_batch_is_one_independent_nats_message(
    nats_url: str,
) -> None:
    server = await nats.connect(servers=[nats_url])
    subject = "wh.rt.batch.farm-a.collector-1.wtg-01"
    received: list[bytes] = []
    subscription = await server.subscribe(subject)

    publisher = CollectorPublisher(
        servers=[nats_url],
        site_id="farm-a",
        collector_id="collector-1",
        delivery="realtime",
        capacity=16,
    )
    await publisher.start()
    # 模拟 10 Hz：三个周期间隔 100ms，必须产生三条独立消息而非合并大批。
    for cycle in range(3):
        await publisher.publish("wtg-01", (_sample(f"p{cycle}"),))
        await asyncio.sleep(0.1)
    await publisher.stop()

    async def collect() -> None:
        while True:
            msg = await subscription.next_msg(timeout=2)
            received.append(msg.data)

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0.5)
    collector.cancel()
    assert len(received) == 3
    batch_ids = {
        SampleBatch.decode(data, "realtime", subject=subject).batch_id for data in received
    }
    assert len(batch_ids) == 3
    await server.drain()
    await server.close()


@pytest.mark.asyncio
async def test_reliable_puback_durable_delivery_and_dlq_routing(nats_url: str) -> None:
    await ensure_history_stream((nats_url,))
    await ensure_dlq_stream((nats_url,))
    subject = "wh.hist.batch.farm-a.collector-1.wtg-02"
    durable = f"sink-{uuid.uuid4().hex[:8]}"
    delivered: list[SampleBatch] = []
    got_batch = asyncio.Event()

    async def handler(batch: SampleBatch) -> None:
        delivered.append(batch)
        got_batch.set()

    consumer = NatsSinkConsumer(
        [nats_url], subject, durable=durable, stream="WH_HISTORY", handler=handler
    )
    await consumer.open()
    try:
        publisher = CollectorPublisher(
            servers=[nats_url],
            site_id="farm-a",
            collector_id="collector-1",
            delivery="reliable",
            capacity=16,
        )
        await publisher.start()
        await publisher.publish("wtg-02", (_sample("p1"),))
        await asyncio.wait_for(got_batch.wait(), timeout=10)
        await publisher.stop()
        assert publisher.pubacked == 1  # PubAck：broker 接收确认
        assert len(delivered) == 1  # 交付确认：下游 handler 独立成立

        # 直接向历史 Stream 注入永久不可解码消息，必须进 DLQ 后 ACK。
        producer = await nats.connect(servers=[nats_url])
        await producer.jetstream().publish(subject, b"{broken json")
        dlq_subscription = await producer.jetstream().subscribe(
            "wh.dlq.batch.>", durable=f"dlq-check-{uuid.uuid4().hex[:8]}", stream="WH_DLQ"
        )
        dlq_msg = await dlq_subscription.next_msg(timeout=10)
        assert dlq_msg.data == b"{broken json"
        assert dlq_msg.headers["Wh-Source-Subject"] == subject
        assert dlq_msg.headers["Wh-Failure-Type"] == "ValueError"
        await dlq_msg.ack()
        await producer.drain()
        await producer.close()
    finally:
        await consumer.close()


@pytest.mark.asyncio
async def test_duplicate_nats_msg_id_does_not_duplicate_delivery(nats_url: str) -> None:
    await ensure_history_stream((nats_url,))
    subject = "wh.hist.batch.farm-a.collector-1.wtg-03"
    durable = f"sink-{uuid.uuid4().hex[:8]}"
    delivered: list[SampleBatch] = []

    async def handler(batch: SampleBatch) -> None:
        delivered.append(batch)

    consumer = NatsSinkConsumer(
        [nats_url], subject, durable=durable, stream="WH_HISTORY", handler=handler
    )
    await consumer.open()
    try:
        publisher = CollectorPublisher(
            servers=[nats_url],
            site_id="farm-a",
            collector_id="collector-1",
            delivery="reliable",
            capacity=16,
        )
        await publisher.start()
        await publisher.publish("wtg-03", (_sample("p1"),))
        await publisher.stop()
        client = await nats.connect(servers=[nats_url])
        batch = SampleBatch(
            batch_id="fixed-id",
            mode="reliable",
            wind_farm_id="farm-a",
            collector_id="collector-1",
            task_id="wtg-03",
            device_id="wtg-03",
            publish_time=datetime.now(UTC),
            samples=(
                # reliable 模式禁止 staleness
                _build_reliable_item("p1"),
            ),
        )
        js = client.jetstream()
        payload = batch.encode()
        await js.publish(subject, payload, headers={"Nats-Msg-Id": "fixed-id"})
        await js.publish(subject, payload, headers={"Nats-Msg-Id": "fixed-id"})
        # publisher 的首批 + 去重后的 fixed-id 批，共两次投递。
        await _drain_event(delivered, 2)
        assert [b.batch_id for b in delivered].count("fixed-id") == 1
        await client.drain()
        await client.close()
    finally:
        await consumer.close()


def _build_reliable_item(point: str):
    from contracts.sample_batch import SampleItem

    now = datetime.now(UTC)
    return SampleItem(
        point_id=point,
        sample_id=uuid.uuid4().hex,
        value=1,
        quality="good",
        sample_time=now,
        max_staleness_ms=None,
    )
