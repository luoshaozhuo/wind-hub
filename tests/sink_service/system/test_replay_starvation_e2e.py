"""§14.3 #40 / §6.3 补发饥饿告警真实端到端系统测试。

设计 v1_22 §6.3：补发等待过长必须生成饥饿事件；可靠 worker 的
``last_error`` 在 backlog>0 且超过 ``replay_starvation_alarm_seconds``
无进展时返回 ``REPLAY_STARVATION: backlog did not progress``，并经
ListDeliveries / GetServiceStatus 向控制面暴露；恢复进展后必须清除。

真实链路：三进程服务 + 真实 NATS JetStream + 真实 Kafka 容器 pause/unpause
故障注入（无 Mock）。通过缩短告警阈值（8s，生产默认 300s）控制用例时长，
不改生产状态机。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from contracts import sink_service_pb2, sink_service_pb2_grpc
from tests.sink_service._stage1_fixtures import SERVICE_FILE
from tests.sink_service.system.conftest import RELIABLE_DELIVERY, SinkSystemHarness

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
    pytest.mark.load,
]

ALARM_SECONDS = 8


def _patch_alarm_threshold(root: Path, seconds: int) -> None:
    path = root / SERVICE_FILE
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["reliable"]["replay_starvation_alarm_seconds"] = seconds
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


async def _delivery_errors(harness: SinkSystemHarness) -> dict[str, str]:
    async with harness.channel() as channel:
        response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest()
        )
    return {item.delivery_id: item.last_error for item in response.deliveries}


class TestReplayStarvationAlarm:
    async def test_starvation_alarm_raises_then_clears_after_recovery(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """Kafka 中断致 backlog 不进展 -> REPLAY_STARVATION 上控制面；恢复排空后清除。"""
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        _patch_alarm_threshold(sink_harness.root, ALARM_SECONDS)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            sink_harness.stack.pause("kafka")
            try:
                batch = sink_harness.make_batch("reliable", seq=1, points=20)
                await sink_harness.publish_reliable(batch)

                # 告警阈值 8s；留出重试/轮询余量，截止 45s 必须看到饥饿信号。
                deadline = asyncio.get_running_loop().time() + 45
                starvation_seen = False
                while asyncio.get_running_loop().time() < deadline:
                    errors = await _delivery_errors(sink_harness)
                    if "REPLAY_STARVATION" in errors.get(RELIABLE_DELIVERY, ""):
                        starvation_seen = True
                        break
                    await asyncio.sleep(1.0)
                assert starvation_seen, (
                    "REPLAY_STARVATION never surfaced while backlog was stuck: "
                    f"{await _delivery_errors(sink_harness)}"
                )

                # 告警期间严禁提前 ACK，且目标侧零确认写入。
                state = await sink_harness.consumer_state()
                assert state["unacked"] is not None and state["unacked"] >= 1
                records = await sink_harness.wait_kafka_messages(min_messages=1, timeout=5)
                assert records == [], "Kafka confirmed writes while the target was paused"
            finally:
                sink_harness.stack.unpause("kafka")

            # 恢复：真实续发到达 Kafka，JetStream 积压排空。
            records = await sink_harness.wait_kafka_messages(min_messages=1, timeout=60)
            assert records, "replay did not reach Kafka after target recovery"

            drain_deadline = asyncio.get_running_loop().time() + 40
            while asyncio.get_running_loop().time() < drain_deadline:
                state = await sink_harness.consumer_state()
                if state["unacked"] == 0:
                    break
                await asyncio.sleep(1.0)
            assert state["unacked"] == 0, f"backlog did not drain: {state}"

            # 进展恢复后饥饿信号必须清除（成功 ACK 路径清空 last_error）。
            clear_deadline = asyncio.get_running_loop().time() + 20
            while asyncio.get_running_loop().time() < clear_deadline:
                errors = await _delivery_errors(sink_harness)
                if not errors.get(RELIABLE_DELIVERY, ""):
                    return
                await asyncio.sleep(1.0)
            raise AssertionError(
                f"REPLAY_STARVATION did not clear after drain: "
                f"{await _delivery_errors(sink_harness)}"
            )
        finally:
            await sink_harness.stop()
