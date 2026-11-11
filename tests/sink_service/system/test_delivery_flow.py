"""§14.4 UC-04/UC-05 双通道派发系统测试（真实 NATS/JetStream/Redis/Kafka）。

所有输入来自独立 NATS 连接发布的冻结 SampleBatch v2，所有确认来自独立
Redis/Kafka 客户端，不读取 sink 进程内部状态：

- UC-04-S01：realtime 批量消息经 Core NATS 到达 Redis 最新值键，值/质量/
  时间戳契约与生产 ``realtime_adapters`` 完全一致；
- UC-04-S02（§14.5.2 确定性断言 1）：同批 good/bad/uncertain 未过期样本
  全部派发且质量标志原样保留；过期样本不写入目标；
- UC-05-S01：reliable 批量消息经 JetStream durable 消费写入 Kafka，独立
  消费者确认后源 Stream 无积压（终局后才 ACK）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from tests.sink_service.system.conftest import (
    COLLECTOR_ID,
    DEVICE_ID,
    WIND_FARM_ID,
    SinkSystemHarness,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.redis,
    pytest.mark.nats,
    pytest.mark.kafka,
    pytest.mark.normal,
    pytest.mark.exception,
]


def _quality_batch(harness: SinkSystemHarness) -> SampleBatch:
    now = datetime.now(UTC)
    return SampleBatch(
        batch_id=f"b-quality-{harness.token}",
        mode="realtime",
        wind_farm_id=WIND_FARM_ID,
        collector_id=COLLECTOR_ID,
        task_id="task-qa",
        device_id=DEVICE_ID,
        publish_time=now,
        samples=(
            SampleItem(
                point_id="q_good",
                sample_id=f"q-good-{harness.token}",
                value=11.0,
                quality="good",
                sample_time=now,
                max_staleness_ms=harness.staleness_ms,
            ),
            SampleItem(
                point_id="q_bad",
                sample_id=f"q-bad-{harness.token}",
                value=22.0,
                quality="bad",
                sample_time=now,
                max_staleness_ms=harness.staleness_ms,
            ),
            SampleItem(
                point_id="q_uncertain",
                sample_id=f"q-uncertain-{harness.token}",
                value=33.0,
                quality="uncertain",
                sample_time=now,
                max_staleness_ms=harness.staleness_ms,
            ),
            SampleItem(
                point_id="q_expired",
                sample_id=f"q-expired-{harness.token}",
                value=44.0,
                quality="good",
                sample_time=datetime.fromtimestamp(
                    now.timestamp() - harness.staleness_ms / 1000.0 - 5, UTC
                ),
                max_staleness_ms=harness.staleness_ms,
            ),
        ),
    )


@pytest.mark.normal
class TestRealtimeDelivery:
    async def test_uc04_s01_realtime_batches_reach_redis(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """Core NATS 批量 -> sink -> Redis 最新值键，时间戳持续推进。"""
        sink_harness.write_dual_channel_root(reliable=False)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            batches = [sink_harness.make_batch("realtime", seq, 10) for seq in range(3)]
            await sink_harness.publish_realtime(batches, interval=0.1)

            key = sink_harness.redis_key("p000")
            # 三批在 0.2s 内发完，首次读取可能已是末批值；先固定当前值，
            # 再显式发一批新样本，对“最新值推进”做确定性断言。
            first = await sink_harness.wait_redis_value(key)
            assert first["quality"] == "good"
            assert first["wind_farm_id"] == WIND_FARM_ID
            assert first["collector_id"] == COLLECTOR_ID
            assert first["device_id"] == DEVICE_ID
            assert first["point_id"] == "p000"
            assert first["sample_time"]

            await sink_harness.publish_realtime([sink_harness.make_batch("realtime", 9, 10)])
            later = await sink_harness.wait_redis_value(
                key, match=lambda item: item["sample_id"] != first["sample_id"]
            )
            assert later["value"] != first["value"] or later["sample_time"] != first["sample_time"]
        finally:
            await sink_harness.stop()

    async def test_uc04_s02_quality_preserved_and_expired_samples_dropped(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """good/bad/uncertain 原样派发；过期样本零写入（§14.5.2 断言 1）。"""
        sink_harness.write_dual_channel_root(reliable=False)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            await sink_harness.publish_realtime([_quality_batch(sink_harness)])

            for point_id, quality in (
                ("q_good", "good"),
                ("q_bad", "bad"),
                ("q_uncertain", "uncertain"),
            ):
                value = await sink_harness.wait_redis_value(
                    sink_harness.redis_key(point_id),
                    match=lambda item, expected=quality: item["quality"] == expected,
                )
                assert value["quality"] == quality

            # 过期点在多个派发周期内必须始终缺席。
            expired_key = sink_harness.redis_key("q_expired")
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                assert expired_key not in await sink_harness.redis_keys(
                    f"{sink_harness.namespace}:*:q_expired"
                )
                await asyncio.sleep(0.5)
        finally:
            await sink_harness.stop()


@pytest.mark.normal
class TestReliableDelivery:
    async def test_uc05_s01_reliable_batch_reaches_kafka_then_source_acked(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """JetStream -> sink -> Kafka 独立确认；终局后源消息才 ACK。"""
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            batch = sink_harness.make_batch("reliable", seq=1, points=20)
            ack = await sink_harness.publish_reliable(batch)
            assert ack.stream == sink_harness.history_stream
            # nats-py 2.16 未设置 duplicate 时字段为 None，而非 False。
            assert not ack.duplicate

            records = await sink_harness.wait_kafka_messages(min_messages=1, timeout=60)
            assert records, "Kafka target did not confirm the reliable batch"
            assert any(record.key for record in records)

            async def _drained() -> bool:
                pending = await sink_harness.stream_pending()
                return pending["durable"] is not None and pending["consumer_pending"] == 0

            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                if await _drained():
                    return
                await asyncio.sleep(1.0)
            pending = await sink_harness.stream_pending()
            raise AssertionError(f"source stream still has unacknowledged messages: {pending}")
        finally:
            await sink_harness.stop()
