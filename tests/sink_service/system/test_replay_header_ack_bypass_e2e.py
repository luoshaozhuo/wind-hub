"""R13（v1_47）伪造 Wh-Replay-Delivery 头不得绕过真实派发（真实 JetStream/Kafka）。

首轮验收的测试缺口的系统级证据：旧实现让任何携带属主不匹配
``Wh-Replay-Delivery`` 头的消息被直接判 terminal 并提前 ACK，目标端从未
收到数据。本用例在真实 NATS JetStream + Kafka KRaft 三进程栈上验证：

1. 带伪造头的可靠批次仍写入 Kafka（头不能跳过派发）；
2. 源 Stream 在 Kafka 确认后才排空（终局后 ACK，无提前确认）；
3. DLQ 无新增（该消息不是无效报文，不得走死信路径）。
"""

from __future__ import annotations

import asyncio

import nats
import pytest

from tests.sink_service.system.conftest import SinkSystemHarness

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.nats,
    pytest.mark.kafka,
    pytest.mark.exception,
]

FORGED_HEADER = {"Wh-Replay-Delivery": "foreign-delivery-id-not-this-one"}


class TestReplayHeaderAckBypass:
    async def test_forged_replay_header_does_not_skip_delivery_or_early_ack(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        sink_harness.write_dual_channel_root()
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            batch = sink_harness.make_batch("reliable", seq=4711, points=5)
            marker = batch.samples[0].sample_id

            client = await nats.connect(servers=sink_harness.stack.nats, connect_timeout=10)
            try:
                js = client.jetstream()
                ack = await js.publish(
                    batch.subject(),
                    batch.encode(),
                    timeout=10,
                    headers=FORGED_HEADER,
                )
                assert ack.stream == sink_harness.history_stream
            finally:
                await client.drain()

            records = await sink_harness.wait_kafka_messages(min_messages=1, timeout=60)
            matched = [record for record in records if marker.encode() in record.value]
            assert matched, (
                "forged Wh-Replay-Delivery header skipped real delivery: "
                f"no Kafka record contains {marker}"
            )

            async def _drained() -> bool:
                pending = await sink_harness.stream_pending()
                return pending["durable"] is not None and pending["consumer_pending"] == 0

            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                if await _drained():
                    break
                await asyncio.sleep(1.0)
            else:
                pending = await sink_harness.stream_pending()
                raise AssertionError(f"forged-header batch not acknowledged in time: {pending}")

            assert (
                await sink_harness.dlq_count() == 0
            ), "valid forged-header batch must not enter DLQ"
        finally:
            await sink_harness.stop()
