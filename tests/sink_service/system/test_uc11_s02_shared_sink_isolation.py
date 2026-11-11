"""§14.4 UC-11-S02 多 Delivery 共享 Sink 故障隔离系统测试（真实三进程，无 Mock）。

两个 reliable Delivery（``power_history`` / ``power_history_aux``）按设备
过滤分流，共享同一个 Kafka Sink 实例（ReliableFactory 按 sink_id 池化物理
目标）。验证：

- 一个 Delivery 被持久 STOP 后，另一个 Delivery 的交付链路全程不受影响、
  无错误记账、无重投；
- STOP 期间积压在被停 Durable 名下，START 后原位排空；
- 两个 Durable 全程并存，Kafka 侧按 sample_id 去重的总量与发布量严格相等
  （共享 Sink 不得产生交叉写入或重复写入）。
"""

from __future__ import annotations

import asyncio
import json as json_lib
import uuid
from typing import Any

import nats
import pytest

from contracts import common_pb2, sink_service_pb2, sink_service_pb2_grpc
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    service_document,
    template_doc,
    template_path,
    write_tree,
)
from tests.sink_service.system.conftest import (
    COLLECTOR_ID,
    KAFKA_SINK,
    WIND_FARM_ID,
    SinkSystemHarness,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.nats,
    pytest.mark.jetstream,
    pytest.mark.kafka,
    pytest.mark.recovery,
    pytest.mark.load,
]

DELIVERY_A = "power_history"
DELIVERY_B = "power_history_aux"
DEVICE_A = "WT001"
DEVICE_B = "WT002"


async def _delivery_view(harness: SinkSystemHarness) -> dict[str, dict[str, str]]:
    async with harness.channel() as channel:
        response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest()
        )
    return {
        item.delivery_id: {
            "state": item.actual_state,
            "last_error": item.last_error,
        }
        for item in response.deliveries
    }


async def _set_delivery(harness: SinkSystemHarness, delivery_id: str, *, start: bool) -> None:
    async with harness.channel() as channel:
        # grpc 生成的方法是 stub 实例属性，不能在类上引用。
        stub = sink_service_pb2_grpc.SinkServiceStub(channel)
        detail = await stub.GetDelivery(common_pb2.ObjectId(id=delivery_id))
        stub_call = stub.StartDelivery if start else stub.StopDelivery
        result = await stub_call(
            common_pb2.OperationRequest(id=delivery_id, expected_revision=detail.delivery.revision)
        )
    assert result.accepted, result.message


async def _wait_state(
    harness: SinkSystemHarness, delivery_id: str, state: str, timeout: float = 30.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last: object = None
    while asyncio.get_running_loop().time() < deadline:
        view = await _delivery_view(harness)
        last = view
        if view.get(delivery_id, {}).get("state") == state:
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"{delivery_id} never reached {state}: {last}")


async def _kafka_sample_ids(harness: SinkSystemHarness, min_messages: int) -> set[str]:
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        harness.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"qa-uc11-{uuid.uuid4().hex[:10]}",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        records: list[Any] = []
        deadline = asyncio.get_running_loop().time() + 45
        while asyncio.get_running_loop().time() < deadline and len(records) < min_messages:
            result = await consumer.getmany(timeout_ms=2000, max_records=500)
            for messages in result.values():
                records.extend(messages)
        return {json_lib.loads(record.value.decode("utf-8"))["sample_id"] for record in records}
    finally:
        await consumer.stop()


async def _sink_durable_names(harness: SinkSystemHarness) -> set[str]:
    client = await nats.connect(servers=harness.stack.nats, connect_timeout=10)
    try:
        infos = await client.jetstream().consumers_info(harness.history_stream)
        return {info.name for info in infos if info.name.startswith("WH_SINK_")}
    finally:
        await client.drain()


def _write_shared_sink_root(harness: SinkSystemHarness) -> None:
    """两个 reliable Delivery 共享唯一 Kafka Sink，按 device 分流。"""
    service = service_document(
        port=harness.grpc_port,
        config_root=str(harness.root),
        callers=[
            {
                "caller_id": "maintenance",
                "certificate_identity": "spiffe://wind-hub/maintenance",
                "permissions": ["query", "control", "config"],
            }
        ],
    )
    service["wind_farm_id"] = WIND_FARM_ID
    service["deployment_id"] = f"qa-{harness.token}"
    service["nats"]["servers"] = [harness.stack.nats]
    service["nats"]["reliable_stream"] = harness.history_stream
    service["nats"]["dead_letter_stream"] = harness.dlq_stream
    service["nats"]["dead_letter_subject"] = harness.dlq_subject

    kafka_template = template_doc("kafka_history")
    kafka_template["connection"]["bootstrap_servers"] = [harness.stack.kafka]
    kafka_template["output"]["topic"] = harness.kafka_topic
    reliable_template = template_doc("reliable_history")
    reliable_template["reliable"]["dead_letter_subject"] = harness.dlq_subject

    def _delivery(device: str) -> dict[str, object]:
        return {
            "template_id": "reliable_history",
            "template_version": "1.0.0",
            "sink_id": KAFKA_SINK,
            "desired_state": "START",
            "source": {
                "wind_farm_id": WIND_FARM_ID,
                "collector_id": COLLECTOR_ID,
                "devices": [device],
                "points": [],
            },
            "overrides": {},
        }

    docs = {
        SERVICE_FILE: service,
        template_path("kafka_history"): kafka_template,
        template_path("reliable_history"): reliable_template,
        SINKS_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "uc11-sinks-r1",
            "sinks": {
                KAFKA_SINK: {
                    "template_id": "kafka_history",
                    "template_version": "1.0.0",
                    "connection": {"bootstrap_servers": [harness.stack.kafka]},
                    "overrides": {},
                }
            },
        },
        DELIVERIES_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "uc11-deliveries-r1",
            "deliveries": {
                DELIVERY_A: _delivery(DEVICE_A),
                DELIVERY_B: _delivery(DEVICE_B),
            },
        },
    }
    write_tree(harness.root, docs)


class TestUC11S02:
    async def test_shared_sink_stop_one_delivery_isolates_the_other(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        """STOP Delivery A 期间 B 持续交付；恢复后 A 原位排空，零交叉零重复。"""
        _write_shared_sink_root(sink_harness)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            await _wait_state(sink_harness, DELIVERY_A, "RUNNING")
            await _wait_state(sink_harness, DELIVERY_B, "RUNNING")
            durables = await _sink_durable_names(sink_harness)
            assert len(durables) == 2, durables

            # 基线双通道各 2 批 × 20 点。
            for seq in range(2):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch("reliable", seq=seq, points=20, device_id=DEVICE_A)
                )
                await sink_harness.publish_reliable(
                    sink_harness.make_batch(
                        "reliable", seq=seq + 100, points=20, device_id=DEVICE_B
                    )
                )
            baseline_ids = await _kafka_sample_ids(sink_harness, min_messages=80)
            assert len(baseline_ids) == 80

            # 故障注入：持久 STOP A；B 必须保持 RUNNING。
            await _set_delivery(sink_harness, DELIVERY_A, start=False)
            await _wait_state(sink_harness, DELIVERY_A, "STOPPED")
            view = await _delivery_view(sink_harness)
            assert view[DELIVERY_B]["state"] == "RUNNING"
            assert view[DELIVERY_B]["last_error"] == ""

            # A 停摆期间：B 继续进；A 的新消息积压、不进 Kafka。
            for seq in range(4):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch(
                        "reliable", seq=seq + 200, points=20, device_id=DEVICE_B
                    )
                )
            await sink_harness.publish_reliable(
                sink_harness.make_batch("reliable", seq=300, points=20, device_id=DEVICE_A)
            )
            b_ids = await _kafka_sample_ids(sink_harness, min_messages=160)
            assert len(b_ids) == 160, f"B leaked or duplicated across shared sink: {len(b_ids)}"
            assert baseline_ids.issubset(b_ids)

            view = await _delivery_view(sink_harness)
            assert view[DELIVERY_B]["state"] == "RUNNING"
            assert view[DELIVERY_B]["last_error"] == ""
            assert len(await _sink_durable_names(sink_harness)) == 2

            # 恢复 A：积压原位排空，总量收敛到 180，无重复无交叉。
            await _set_delivery(sink_harness, DELIVERY_A, start=True)
            await _wait_state(sink_harness, DELIVERY_A, "RUNNING")
            final_ids = await _kafka_sample_ids(sink_harness, min_messages=180)
            assert len(final_ids) == 180, f"duplicate/missing after recovery: {len(final_ids)}"
            assert b_ids.issubset(final_ids)

            state_a = await sink_harness.consumer_state()
            assert state_a["unacked"] == 0
            assert state_a["num_redelivered"] == 0
        finally:
            await sink_harness.stop()
