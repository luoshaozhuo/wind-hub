"""L1 分区缓存生产化的真实链路系统测试（r5_stack，无 Mock）。

在真实 Kafka/JetStream 与真实三进程 Sink Service 上验证：

- 不同设备 Key 的分区分布与 aiokafka 默认 partitioner 解析结果严格一致，
  且同一 Key 多批稳定落在同一分区（缓存命中不改写 Key、不人为指定单分区）；
- 主题 6→8 在线扩容后，生产侧 metadata listener 清空缓存并按新拓扑重解析，
  新分区（6/7）在 metadata 刷新窗口内收到写入，全程零错误零重投；
- 服务 stop/start（producer close/reopen）后监听链路重建，分区选择保持一致。

扩分区后的元数据传播依赖 aiokafka producer 的周期性 metadata 刷新
（默认 metadata_max_age_ms=300000），测试以 330s 为硬截止，属 slow 用例；
不通过修改生产状态机加速。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewPartitions, NewTopic

from contracts import sink_service_pb2, sink_service_pb2_grpc
from tests.sink_service._stage1_fixtures import template_path
from tests.sink_service.system.conftest import (
    COLLECTOR_ID,
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

REFRESH_DEADLINE_S = 330.0
DEVICES = [f"WT{idx:03d}" for idx in range(1, 9)]


def _device_key(device_id: str) -> bytes:
    return ":".join((WIND_FARM_ID, COLLECTOR_ID, device_id)).encode("utf-8")


async def _ensure_topic(bootstrap: str, topic: str, partitions: int) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    try:
        await admin.start()
        existing = await admin.list_topics()
        if topic not in existing:
            await admin.create_topics(
                [NewTopic(name=topic, num_partitions=partitions, replication_factor=1)]
            )
    finally:
        await admin.close()


async def _expand_topic(bootstrap: str, topic: str, total_partitions: int) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    try:
        await admin.start()
        await admin.create_partitions({topic: NewPartitions(total_count=total_partitions)})
    finally:
        await admin.close()


async def _reference_partitions(bootstrap: str, topic: str, devices: list[str]) -> dict[str, int]:
    """用独立 producer 走默认 partitioner 给出权威分区期望（测试侧对照）。"""
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    try:
        await producer.start()
        await producer.client._wait_on_metadata(topic)  # noqa: SLF001
        result: dict[str, int] = {}
        for device in devices:
            key = _device_key(device)
            result[device] = producer._partition(  # noqa: SLF001
                topic, None, key, None, key, None
            )
        return result
    finally:
        await producer.stop()


def _open_consumer(bootstrap: str, topic: str) -> AIOKafkaConsumer:
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        auto_offset_reset="earliest",
        group_id=f"qa-l1-{uuid.uuid4().hex[:10]}",
        enable_auto_commit=False,
    )
    return consumer


def _open_assigned_consumer(bootstrap: str, topic: str, partitions: list[int]) -> AIOKafkaConsumer:
    """无 group 手动 assign consumer：扩分区期间不依赖 group coordinator。

    第一轮实测 subscribe+group 模式在 admin 扩分区后出现 coordinator 失效，
    getmany 瞬时返空导致轮询空转；手动 assign 只走 metadata 通道，节拍由
    测试侧显式 sleep 保证。允许包含当前尚不存在的分区（fetcher 后台重试，
    metadata 刷新后自动接管），避免扩容时刻 reassign/seek 与 fetcher 重置
    竞争——第三轮实测该竞争会让 getmany 永久挂起。
    """
    from aiokafka.structs import TopicPartition

    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    consumer.assign([TopicPartition(topic, partition) for partition in partitions])
    return consumer


async def _drain(
    consumer: AIOKafkaConsumer,
    min_messages: int,
    deadline_s: float,
    *,
    poll_timeout_ms: int = 2000,
) -> list[Any]:
    records: list[Any] = []
    deadline = asyncio.get_running_loop().time() + deadline_s
    while True:
        result = await consumer.getmany(timeout_ms=poll_timeout_ms, max_records=500)
        for messages in result.values():
            records.extend(messages)
        if len(records) >= min_messages or asyncio.get_running_loop().time() >= deadline:
            return records


def _retarget_kafka_topic(harness: SinkSystemHarness, topic: str) -> None:
    path = Path(harness.root) / template_path("kafka_history")
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["output"]["topic"] = topic
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


async def _delivery_view(harness: SinkSystemHarness) -> dict[str, dict[str, str]]:
    async with harness.channel() as channel:
        response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest()
        )
    return {
        item.delivery_id: {"state": item.actual_state, "last_error": item.last_error}
        for item in response.deliveries
    }


async def _wait_backlog_zero(harness: SinkSystemHarness, timeout_s: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        state = await harness.consumer_state()
        if state["unacked"] == 0:
            return
        await asyncio.sleep(1.0)
    raise AssertionError(f"backlog did not drain: {await harness.consumer_state()}")


class TestKafkaPartitionCacheE2E:
    async def test_partition_distribution_matches_default_partitioner(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        topic = f"wh-l1-dist-{sink_harness.token}"
        await _ensure_topic(sink_harness.stack.kafka, topic, 6)
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        _retarget_kafka_topic(sink_harness, topic)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            expected = await _reference_partitions(sink_harness.stack.kafka, topic, DEVICES)
            assert len(set(expected.values())) > 1, "测试前提：8 个 Key 必须分布到多分区"

            # 8 设备 × 2 批 × 20 点 = 320 条；seq 全局唯一，避免 batch/sample 标识碰撞。
            for round_idx in range(2):
                for device_idx, device in enumerate(DEVICES):
                    await sink_harness.publish_reliable(
                        sink_harness.make_batch(
                            "reliable",
                            seq=round_idx * 100 + device_idx,
                            points=20,
                            device_id=device,
                        )
                    )
            consumer = _open_consumer(sink_harness.stack.kafka, topic)
            await consumer.start()
            try:
                records = await _drain(consumer, 320, deadline_s=45.0)
            finally:
                await consumer.stop()
            assert len(records) == 320
            observed: dict[str, set[int]] = {}
            per_device: dict[str, int] = {}
            for record in records:
                payload = json.loads(record.value.decode("utf-8"))
                device = payload["device_id"]
                observed.setdefault(device, set()).add(record.partition)
                per_device[device] = per_device.get(device, 0) + 1
            assert per_device == {device: 40 for device in DEVICES}
            for device, partitions in observed.items():
                assert partitions == {expected[device]}, (device, partitions, expected[device])

            await _wait_backlog_zero(sink_harness)
            state = await sink_harness.consumer_state()
            assert state["num_redelivered"] == 0
            for view in (await _delivery_view(sink_harness)).values():
                assert view["state"] == "RUNNING" and view["last_error"] == ""
        finally:
            await sink_harness.stop()

    async def test_topic_expansion_invalidates_cache_and_uses_new_partitions(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        topic = f"wh-l1-expand-{sink_harness.token}"
        await _ensure_topic(sink_harness.stack.kafka, topic, 6)
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        _retarget_kafka_topic(sink_harness, topic)
        await sink_harness.provision_streams()
        await sink_harness.start()
        try:
            phase1_devices = [f"WT{idx:03d}" for idx in range(10, 14)]
            expected6 = await _reference_partitions(sink_harness.stack.kafka, topic, phase1_devices)
            for seq, device in enumerate(phase1_devices):
                for round_idx in range(2):
                    await sink_harness.publish_reliable(
                        sink_harness.make_batch(
                            "reliable",
                            seq=200 + seq * 2 + round_idx,
                            points=20,
                            device_id=device,
                        )
                    )
            # 手动 assign 且启动即覆盖 8 个分区（6/7 此刻尚不存在，fetcher 后台
            # 重试，元数据刷新后自动接管）。不使用 consumer group：第一轮实测
            # 扩分区会使 group coordinator 短暂失效、getmany 瞬时返空导致空转；
            # 也避免中途 reassign/seek——第三轮实测二者与 fetcher 重置竞争会挂死。
            consumer = _open_assigned_consumer(sink_harness.stack.kafka, topic, list(range(8)))
            await consumer.start()
            all_records: list[Any] = []
            try:
                phase1 = await _drain(consumer, 160, deadline_s=45.0)
                all_records.extend(phase1)
                assert len(phase1) == 160
                assert all(record.partition < 6 for record in phase1)
                phase1_partitions: dict[str, set[int]] = {}
                for record in phase1:
                    payload = json.loads(record.value.decode("utf-8"))
                    phase1_partitions.setdefault(payload["device_id"], set()).add(record.partition)
                for device, partition in expected6.items():
                    assert phase1_partitions[device] == {partition}

                # 在线 6→8 扩容；consumer 无需任何重配，位点连续不重复。
                await _expand_topic(sink_harness.stack.kafka, topic, 8)

                # 在 8 分区拓扑下挑选会落到新分区 6/7 的设备（独立权威 producer）。
                candidates = [f"WT{idx:03d}" for idx in range(100, 130)]
                expected8 = await _reference_partitions(sink_harness.stack.kafka, topic, candidates)
                remapped = [
                    device for device, partition in expected8.items() if partition in (6, 7)
                ]
                assert remapped, "候选 Key 中无映射到新分区的设备，扩大候选集"
                target = remapped[0]

                # 3s 显式节拍持续灌入，直到 metadata 刷新 + listener 失效后重解析
                # 到新分区。节拍不依赖 getmany 阻塞，迭代数有硬上界。
                poll_interval_s = 3.0
                max_iters = int(REFRESH_DEADLINE_S // poll_interval_s) + 1
                seq = 0
                saw_new_partition = False
                for _ in range(max_iters):
                    await sink_harness.publish_reliable(
                        sink_harness.make_batch(
                            "reliable", seq=300 + seq, points=20, device_id=target
                        )
                    )
                    seq += 1
                    await asyncio.sleep(poll_interval_s)
                    batch = await _drain(consumer, 0, deadline_s=1.0, poll_timeout_ms=500)
                    all_records.extend(batch)
                    for record in batch:
                        payload = json.loads(record.value.decode("utf-8"))
                        if payload["device_id"] == target and record.partition in (6, 7):
                            saw_new_partition = True
                    if saw_new_partition:
                        break
                assert saw_new_partition, (
                    f"扩容后 {REFRESH_DEADLINE_S}s 内缓存未失效/未使用新分区，"
                    "listener 链路可能断裂"
                )

                # 收敛核对：独立总量精确、零重投、服务健康。
                remaining = await _drain(consumer, 0, deadline_s=15.0)
                all_records.extend(remaining)
            finally:
                await consumer.stop()
            target_records = [
                record
                for record in all_records
                if json.loads(record.value.decode("utf-8"))["device_id"] == target
            ]
            assert len(target_records) == seq * 20
            assert any(record.partition in (6, 7) for record in target_records)
            assert len(all_records) == 160 + seq * 20
            state = await sink_harness.consumer_state()
            assert state["num_redelivered"] == 0
            for view in (await _delivery_view(sink_harness)).values():
                assert view["state"] == "RUNNING" and view["last_error"] == ""
        finally:
            await sink_harness.stop()

    async def test_restart_reopens_producer_and_keeps_partition_stable(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        topic = f"wh-l1-restart-{sink_harness.token}"
        await _ensure_topic(sink_harness.stack.kafka, topic, 6)
        sink_harness.write_dual_channel_root(ack_wait_seconds=10)
        _retarget_kafka_topic(sink_harness, topic)
        await sink_harness.provision_streams()
        device = "WT200"
        await sink_harness.start()
        try:
            expected = (await _reference_partitions(sink_harness.stack.kafka, topic, [device]))[
                device
            ]
            for seq in range(2):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch("reliable", seq=seq, points=20, device_id=device)
                )
            consumer = _open_consumer(sink_harness.stack.kafka, topic)
            await consumer.start()
            try:
                before = await _drain(consumer, 40, deadline_s=45.0)
            finally:
                await consumer.stop()
            assert len(before) == 40
            assert {record.partition for record in before} == {expected}
            durable_before = (await sink_harness.consumer_state())["durable"]
            assert durable_before is not None
            assert await sink_harness.stop() == 0
        finally:
            if sink_harness.process is not None:
                await sink_harness.stop()

        # close/reopen：新 producer + listener 重新注册，Durable 身份不变。
        await sink_harness.start()
        try:
            for seq in range(2, 4):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch("reliable", seq=seq, points=20, device_id=device)
                )
            consumer = _open_consumer(sink_harness.stack.kafka, topic)
            await consumer.start()
            try:
                after = await _drain(consumer, 80, deadline_s=45.0)
            finally:
                await consumer.stop()
            assert len(after) == 80
            assert {record.partition for record in after} == {expected}
            state = await sink_harness.consumer_state()
            assert state["durable"] == durable_before
            assert state["num_redelivered"] == 0
            assert state["unacked"] == 0
        finally:
            await sink_harness.stop()
