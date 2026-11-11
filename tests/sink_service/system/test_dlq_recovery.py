"""§14.4 UC-05/UC-10/UC-12 死信、重投与故障恢复系统测试（真实注入，无 Mock）。

Round-3 S3 优先级场景收口，全部基于真实 NATS JetStream / Kafka 与真实
三进程服务：

- UC-05-S03 正常死信路径：畸形可靠批经 DLQ Stream 持久确认后源消息才 ACK；
- S3-01 / S3-09：配额受限的 DLQ Stream 满后 PubAck 失败，源消息禁止提前
  ACK、禁止静默丢弃——表现为 durable 持续积压、服务端重投计数增长、
  事件流留下 ERROR 证据；
- S3-02 / S3-08：Kafka 容器 pause 制造结果不确定的目标中断，期间无任何
  成功确认与新 Kafka 记录、JetStream 积压保持；恢复后自动续发并排空；
- S3-03：NATS 线协议代理丢弃 ``$JS.ACK`` 帧，服务端按 ack_wait 重投，
  Kafka 侧实证 at-least-once 重复（业务策略 allow_at_least_once）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.sink_service.system.conftest import SinkSystemHarness
from tests.sink_service.system.nats_fault_proxy import NatsFaultProxy, drop_jetstream_ack

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.nats,
    pytest.mark.jetstream,
    pytest.mark.kafka,
    pytest.mark.exception,
    pytest.mark.timeout,
    pytest.mark.recovery,
]


class TestDeadLetterAndNoEarlyAck:
    async def test_uc05_s03_invalid_batch_persisted_to_dlq_then_source_acked(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """畸形批先持久进 DLQ，再确认源 ACK；DLQ 内容可独立拉取。"""
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            await sink_harness.publish_raw_reliable(b"{not-a-valid-batch")

            async def _dlq_seen() -> bool:
                return (await sink_harness.dlq_count()) >= 1

            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                if await _dlq_seen():
                    break
                await asyncio.sleep(1.0)
            record = await sink_harness.dlq_latest()
            assert record is not None, "invalid batch never reached the DLQ stream"
            envelope = json.loads(record.data.decode())
            assert envelope["classification"] == "PERMANENT"
            assert envelope["delivery_id"]

            async def _acked() -> bool:
                state = await sink_harness.consumer_state()
                return state["unacked"] == 0

            ack_deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < ack_deadline:
                if await _acked():
                    return
                await asyncio.sleep(1.0)
            raise AssertionError("source message was not ACKed after durable DLQ PubAck")
        finally:
            await sink_harness.stop()

    async def test_s3_01_s3_09_full_dlq_puback_failure_never_early_acks_or_silently_drops(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """DLQ 满 -> PubAck 失败：源不 ACK、重投增长、DLQ 不被覆盖、事件留痕。"""
        sink_harness.write_dual_channel_root(ack_wait_seconds=5)
        # DLQ 仅容纳 1 条：第一条畸形批占满，第二条必然 PubAck 失败。
        await sink_harness.provision_streams(dlq_max_msgs=1)
        await sink_harness.start()
        try:
            await sink_harness.publish_raw_reliable(b"{first-permanent-failure")
            first_deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < first_deadline:
                if await sink_harness.dlq_count() == 1:
                    break
                await asyncio.sleep(1.0)
            dlq_before = await sink_harness.dlq_latest()
            assert dlq_before is not None

            await sink_harness.publish_raw_reliable(b"{second-permanent-failure")

            # 跨越至少两个 ack_wait 周期观察：持续积压且重投计数单调增长。
            await asyncio.sleep(13.0)
            state = await sink_harness.consumer_state()
            assert state["durable"] is not None
            assert state["unacked"] is not None and state["unacked"] >= 1
            assert state["num_redelivered"] is not None and state["num_redelivered"] >= 1

            # DLQ 配额（discard=new）下旧记录不得被静默覆盖。
            assert await sink_harness.dlq_count() == 1
            dlq_after = await sink_harness.dlq_latest()
            assert dlq_after is not None
            assert dlq_after.data == dlq_before.data

            events = await sink_harness.events()
            assert any(
                event.severity == "ERROR" for event in events
            ), "DLQ PubAck failure left no ERROR event evidence"
        finally:
            await sink_harness.stop()


class TestTargetOutageAndReplay:
    async def test_s3_02_s3_08_kafka_outage_holds_backlog_then_replays_drain(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """Kafka pause 期间零成功确认、积压保持；恢复后续发排空。"""
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            sink_harness.stack.pause("kafka")
            try:
                batch = sink_harness.make_batch("reliable", seq=1, points=20)
                await sink_harness.publish_reliable(batch)

                # 覆盖多个重试/超时窗口：目标侧零新记录、源侧持续未 ACK。
                await asyncio.sleep(15.0)
                state = await sink_harness.consumer_state()
                assert state["unacked"] is not None and state["unacked"] >= 1
                records = await sink_harness.wait_kafka_messages(min_messages=1, timeout=5)
                assert records == [], "Kafka confirmed writes while the target was paused"
            finally:
                sink_harness.stack.unpause("kafka")

            records = await sink_harness.wait_kafka_messages(min_messages=1, timeout=60)
            assert records, "replay did not reach Kafka after target recovery"

            async def _drained() -> bool:
                state = await sink_harness.consumer_state()
                return state["unacked"] == 0

            deadline = asyncio.get_running_loop().time() + 40
            while asyncio.get_running_loop().time() < deadline:
                if await _drained():
                    return
                await asyncio.sleep(1.0)
            raise AssertionError(
                f"backlog did not drain after Kafka recovery: {await sink_harness.consumer_state()}"
            )
        finally:
            await sink_harness.stop()


class TestAckLossRedelivery:
    async def test_s3_03_dropped_jetstream_ack_causes_at_least_once_redelivery(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """ACK 帧在线路丢失 -> 服务端重投 -> Kafka 出现同 sample_id 重复。"""
        host, _, port = sink_harness.stack.nats.removeprefix("nats://").rpartition(":")
        proxy = NatsFaultProxy(host, int(port), drop=drop_jetstream_ack)
        await proxy.start()
        try:
            sink_harness.write_dual_channel_root(nats_url=proxy.address, ack_wait_seconds=5)
            await sink_harness.provision_streams()
            await sink_harness.start()
            try:
                batch = sink_harness.make_batch("reliable", seq=7, points=5)
                sample_ids = {sample.sample_id for sample in batch.samples}
                await sink_harness.publish_reliable(batch)

                async def _duplicates_seen() -> bool:
                    records = await sink_harness.wait_kafka_messages(
                        min_messages=len(batch.samples) + 1, timeout=8
                    )
                    if len(records) <= len(batch.samples):
                        return False
                    payloads = [json.loads(record.value.decode()) for record in records]
                    observed = {item["sample_id"] for item in payloads}
                    return sample_ids.issubset(observed)

                deadline = asyncio.get_running_loop().time() + 70
                while asyncio.get_running_loop().time() < deadline:
                    if await _duplicates_seen():
                        assert proxy.dropped_frames >= 1
                        state = await sink_harness.consumer_state()
                        assert state["num_redelivered"] is not None
                        assert state["num_redelivered"] >= 1
                        return
                    await asyncio.sleep(2.0)
                raise AssertionError(
                    f"no at-least-once duplicate observed; proxy drops={proxy.dropped_frames}, "
                    f"state={await sink_harness.consumer_state()}"
                )
            finally:
                await sink_harness.stop()
        finally:
            await proxy.stop()
