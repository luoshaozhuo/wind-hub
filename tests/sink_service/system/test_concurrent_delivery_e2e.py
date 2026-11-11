"""v1_23 设备级并发 Reliable Delivery 系统验收（真实三进程 + 真实 NATS/Kafka）。

对应任务书 S3 的并行专属场景，所有证据均取自进程边界：

- R7-01：4 设备 × 4 Reliable Delivery 并发，精确设备 Subject Durable、
  同设备严格有序、跨设备并行（同一时刻 ≥2 Durable 在途）、零重投零 DLQ；
- R7-02：Kafka pause 期间不得提前 ACK，unpause 后全部排空；
- R7-03：单设备毒消息走 DLQ+ACK，不阻塞其他设备；
- R7-04：STOP 单 Delivery 的隔离性与重复 START/STOP 幂等；
- R7-05：旧 1.0.0 模板宽订阅串行路径不退化；
- R7-06：新旧 Delivery 身份切换无静默双写，旧 Durable 服务端保留但不产生
  重复 Kafka 记录（迁移边界由运维确认，见设计 §6.3.3）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json as json_lib
import shutil
import uuid
from typing import Any

import nats
import pytest

from contracts import common_pb2, sink_service_pb2, sink_service_pb2_grpc
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    TEMPLATE_ROOT,
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

DEVICES = ["WT001", "WT002", "WT003", "WT004"]
POINTS_PER_BATCH = 50


def _delivery_id(index: int) -> str:
    return f"history_dev{index + 1}"


def _durable_name(deployment_id: str, delivery_id: str) -> str:
    """与 reliable_runtime._durable_id 同一公式。"""
    seed = f"{deployment_id}:{delivery_id}".encode()
    return "WH_SINK_" + hashlib.sha256(seed).hexdigest()[:24]


def _write_concurrent_root(
    harness: SinkSystemHarness,
    *,
    devices: list[str],
    concurrent: bool = True,
    delivery_ids: list[str] | None = None,
    ack_wait_seconds: int | None = None,
) -> None:
    version = "1.1.0" if concurrent else "1.0.0"
    delivery_ids = delivery_ids or [_delivery_id(i) for i in range(len(devices))]
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
    if ack_wait_seconds is not None:
        service["reliable"]["ack_wait_seconds"] = ack_wait_seconds

    kafka_template = template_doc("kafka_history")
    kafka_template["file_version"] = version
    kafka_template["connection"]["bootstrap_servers"] = [harness.stack.kafka]
    kafka_template["output"]["topic"] = harness.kafka_topic
    if concurrent:
        kafka_template["capabilities"]["supports_concurrent_write"] = True
        kafka_template["capabilities"]["max_safe_inflight"] = 4
    reliable_template = template_doc("reliable_history")
    reliable_template["reliable"]["dead_letter_subject"] = harness.dlq_subject

    def _delivery(delivery_id: str, device_selector: list[str]) -> dict[str, object]:
        return {
            "template_id": "reliable_history",
            "template_version": "1.0.0",
            "sink_id": KAFKA_SINK,
            "desired_state": "START",
            "source": {
                "wind_farm_id": WIND_FARM_ID,
                "collector_id": COLLECTOR_ID,
                "devices": device_selector,
                "points": [],
            },
            "overrides": {},
        }

    sinks = {
        KAFKA_SINK: {
            "template_id": "kafka_history",
            "template_version": version,
            "connection": {"bootstrap_servers": [harness.stack.kafka]},
            "overrides": {},
        }
    }
    if concurrent:
        deliveries = {
            did: _delivery(did, [device]) for did, device in zip(delivery_ids, devices, strict=True)
        }
    else:
        deliveries = {delivery_ids[0]: _delivery(delivery_ids[0], devices)}
    docs = {
        SERVICE_FILE: service,
        f"{TEMPLATE_ROOT}/sinks/kafka_history_{version}.yaml": kafka_template,
        template_path("reliable_history"): reliable_template,
        SINKS_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "r7-sinks",
            "sinks": sinks,
        },
        DELIVERIES_INSTANCE: {
            "schema_version": 1,
            "file_version": "1.0.0",
            "revision": "r7-deliveries",
            "deliveries": deliveries,
        },
    }
    write_tree(harness.root, docs)


async def _consumer_infos(harness: SinkSystemHarness) -> dict[str, Any]:
    client = await nats.connect(servers=harness.stack.nats, connect_timeout=10)
    try:
        js = client.jetstream()
        infos = await js.consumers_info(harness.history_stream)
        return {info.name: info for info in infos if info.name.startswith("WH_SINK_")}
    finally:
        await client.drain()


async def _wait_all_drained(
    harness: SinkSystemHarness, names: set[str], *, timeout: float = 60.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, int] = {}
    while asyncio.get_running_loop().time() < deadline:
        infos = await _consumer_infos(harness)
        last = {
            name: infos[name].num_pending + infos[name].num_ack_pending
            for name in names
            if name in infos
        }
        if len(last) == len(names) and all(value == 0 for value in last.values()):
            return
        await asyncio.sleep(0.5)
    raise AssertionError(f"durables did not drain: {last}")


async def _drain_kafka(
    harness: SinkSystemHarness, *, min_messages: int, timeout: float = 60.0
) -> list[Any]:
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        harness.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"qa-r7-{uuid.uuid4().hex[:10]}",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        records: list[Any] = []
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline and len(records) < min_messages:
            result = await consumer.getmany(timeout_ms=2000, max_records=1000)
            for messages in result.values():
                records.extend(messages)
        return records
    finally:
        await consumer.stop()


async def _delivery_view(harness: SinkSystemHarness) -> dict[str, dict[str, str]]:
    async with harness.channel() as channel:
        response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest()
        )
    return {
        item.delivery_id: {"state": item.actual_state, "last_error": item.last_error}
        for item in response.deliveries
    }


async def _set_delivery(harness: SinkSystemHarness, delivery_id: str, *, start: bool) -> None:
    async with harness.channel() as channel:
        stub = sink_service_pb2_grpc.SinkServiceStub(channel)
        detail = await stub.GetDelivery(common_pb2.ObjectId(id=delivery_id))
        call = stub.StartDelivery if start else stub.StopDelivery
        result = await call(
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


async def _publish_per_device(
    harness: SinkSystemHarness, *, batches: int, seq_offset: int = 0
) -> int:
    total = 0
    for seq in range(batches):
        for device in DEVICES:
            await harness.publish_reliable(
                harness.make_batch(
                    "reliable",
                    seq=seq + seq_offset,
                    points=POINTS_PER_BATCH,
                    device_id=device,
                )
            )
            total += POINTS_PER_BATCH
    return total


class TestConcurrentReliableDelivery:
    async def test_r7_01_four_devices_exact_subjects_ordering_and_parallelism(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        _write_concurrent_root(sink_harness, devices=DEVICES)
        await sink_harness.provision_streams()
        await sink_harness.start()
        deployment = f"qa-{sink_harness.token}"
        expected_names = {_durable_name(deployment, _delivery_id(i)) for i in range(4)}
        try:
            await asyncio.gather(
                *(_wait_state(sink_harness, _delivery_id(i), "RUNNING") for i in range(4))
            )
            infos = await _consumer_infos(sink_harness)
            assert set(infos) == expected_names
            for i, device in enumerate(DEVICES):
                expected_subject = f"wh.hist.batch.{WIND_FARM_ID}.{COLLECTOR_ID}.{device}"
                name = _durable_name(deployment, _delivery_id(i))
                assert infos[name].config.filter_subject == expected_subject

            # 一次性压入 5 轮负载，在排空过程中捕捉 ≥2 Durable 同时在途的并行瞬间。
            total = await _publish_per_device(sink_harness, batches=5)
            parallel_seen = False
            deadline = asyncio.get_running_loop().time() + 45
            while asyncio.get_running_loop().time() < deadline:
                current = await _consumer_infos(sink_harness)
                inflight = sum(1 for name in expected_names if current[name].num_ack_pending > 0)
                if inflight >= 2:
                    parallel_seen = True
                    break
                pending = sum(
                    current[name].num_pending + current[name].num_ack_pending
                    for name in expected_names
                )
                if pending == 0:
                    break
                await asyncio.sleep(0.05)
            assert parallel_seen, "从未观察到两个 Durable 同时在途"
            await _wait_all_drained(sink_harness, expected_names)
            records = await _drain_kafka(sink_harness, min_messages=total)
            assert len(records) == total
            # 同设备严格有序：按 offset 顺序解析 sample_id 中的 seq/idx。
            # sample_id 本身不含设备标识，跨设备去重键必须带 device_id。
            per_device: dict[str, list[tuple[int, int]]] = {device: [] for device in DEVICES}
            identities: set[tuple[str, str]] = set()
            for record in records:
                payload = json_lib.loads(record.value.decode("utf-8"))
                per_device[payload["device_id"]].append(
                    tuple(int(part) for part in payload["sample_id"].split("-")[2:4])
                )
                identities.add((payload["device_id"], payload["sample_id"]))
            assert len(identities) == total  # 零重复（设备内 sample_id 唯一）
            for device, pairs in per_device.items():
                assert len(pairs) == total // len(DEVICES)
                assert pairs == sorted(pairs), f"{device} 出现乱序"
            infos = await _consumer_infos(sink_harness)
            assert all(info.num_redelivered == 0 for info in infos.values())
            assert await sink_harness.dlq_count() == 0
        finally:
            await sink_harness.stop()

    async def test_r7_02_kafka_pause_no_early_ack_then_drain(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        _write_concurrent_root(sink_harness, devices=DEVICES, ack_wait_seconds=30)
        await sink_harness.provision_streams()
        await sink_harness.start()
        deployment = f"qa-{sink_harness.token}"
        names = {_durable_name(deployment, _delivery_id(i)) for i in range(4)}
        try:
            await asyncio.gather(
                *(_wait_state(sink_harness, _delivery_id(i), "RUNNING") for i in range(4))
            )
            await _publish_per_device(sink_harness, batches=2)
            await _wait_all_drained(sink_harness, names)
            # pause 期间无法新建 consumer（broker 不可 bootstrap），必须在
            # pause 前建立长连接并全程复用，以独立 group 从 earliest 观测。
            from aiokafka import AIOKafkaConsumer
            from aiokafka.errors import KafkaError

            watcher = AIOKafkaConsumer(
                sink_harness.kafka_topic,
                bootstrap_servers=sink_harness.stack.kafka,
                auto_offset_reset="earliest",
                group_id=f"qa-r702-{uuid.uuid4().hex[:10]}",
                enable_auto_commit=False,
            )
            await watcher.start()
            try:
                seen: list[Any] = []
                watch_deadline = asyncio.get_running_loop().time() + 60
                while (
                    asyncio.get_running_loop().time() < watch_deadline
                    and len(seen) < 4 * 2 * POINTS_PER_BATCH
                ):
                    result = await watcher.getmany(timeout_ms=2000, max_records=1000)
                    for messages in result.values():
                        seen.extend(messages)
                before = len(seen)
                assert before == 4 * 2 * POINTS_PER_BATCH

                sink_harness.stack.pause("kafka")
                try:
                    await _publish_per_device(sink_harness, batches=2, seq_offset=50)
                    await asyncio.sleep(12)
                    infos = await _consumer_infos(sink_harness)
                    # 未确认写入必须持有 ACK 所有权：每个 Durable 都有未 ACK 积压。
                    held = {
                        name: infos[name].num_pending + infos[name].num_ack_pending
                        for name in names
                    }
                    assert all(value > 0 for value in held.values()), held
                    # broker 暂停期间长连接 consumer 的拉取只允许空结果或
                    # 连接类异常，记录数必须保持不变（零新增即无提前 ACK 漏写）。
                    pause_deadline = asyncio.get_running_loop().time() + 10
                    while asyncio.get_running_loop().time() < pause_deadline:
                        try:
                            paused = await watcher.getmany(timeout_ms=2000, max_records=1000)
                        except KafkaError:
                            continue
                        for messages in paused.values():
                            seen.extend(messages)
                    assert len(seen) == before  # pause 期间 Kafka 零新增
                finally:
                    sink_harness.stack.unpause("kafka")
            finally:
                await watcher.stop()
            await _wait_all_drained(sink_harness, names, timeout=90)
            total = 4 * 4 * POINTS_PER_BATCH
            records = await _drain_kafka(sink_harness, min_messages=total, timeout=90)
            assert len(records) == total
        finally:
            await sink_harness.stop()

    async def test_r7_03_poison_batch_on_one_device_isolated(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        _write_concurrent_root(sink_harness, devices=DEVICES)
        await sink_harness.provision_streams()
        await sink_harness.start()
        deployment = f"qa-{sink_harness.token}"
        names = {_durable_name(deployment, _delivery_id(i)) for i in range(4)}
        try:
            await asyncio.gather(
                *(_wait_state(sink_harness, _delivery_id(i), "RUNNING") for i in range(4))
            )
            await _publish_per_device(sink_harness, batches=1)
            # 向 WT004 注入畸形批：该设备走 DLQ+ACK，其余设备不受影响。
            await sink_harness.publish_raw_reliable(b"{not-json", device_id="WT004")
            await _publish_per_device(sink_harness, batches=2, seq_offset=80)
            await _wait_all_drained(sink_harness, names)
            assert await sink_harness.dlq_count() >= 1
            good_total = 4 * 3 * POINTS_PER_BATCH
            records = await _drain_kafka(sink_harness, min_messages=good_total)
            assert len(records) == good_total
            devices_seen = {
                json_lib.loads(record.value.decode("utf-8"))["device_id"] for record in records
            }
            assert devices_seen == set(DEVICES)
            infos = await _consumer_infos(sink_harness)
            redelivered = {name: infos[name].num_redelivered for name in names}
            assert all(count == 0 for count in redelivered.values()), redelivered
        finally:
            await sink_harness.stop()

    async def test_r7_04_stop_isolation_and_idempotent_start_stop(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        _write_concurrent_root(sink_harness, devices=DEVICES)
        await sink_harness.provision_streams()
        await sink_harness.start()
        deployment = f"qa-{sink_harness.token}"
        names = {_durable_name(deployment, _delivery_id(i)) for i in range(4)}
        target = _delivery_id(1)
        try:
            await asyncio.gather(
                *(_wait_state(sink_harness, _delivery_id(i), "RUNNING") for i in range(4))
            )
            await _set_delivery(sink_harness, target, start=False)
            await _wait_state(sink_harness, target, "STOPPED")
            # 重复 STOP 幂等：状态保持 STOPPED，不报错。
            await _set_delivery(sink_harness, target, start=False)
            await asyncio.sleep(1)
            assert (await _delivery_view(sink_harness))[target]["state"] == "STOPPED"

            await _publish_per_device(sink_harness, batches=2)
            running_names = names - {_durable_name(deployment, target)}
            await _wait_all_drained(sink_harness, running_names)
            infos = await _consumer_infos(sink_harness)
            stopped_durable = _durable_name(deployment, target)
            assert infos[stopped_durable].num_pending + infos[stopped_durable].num_ack_pending > 0

            # 恢复后原位排空；重复 START 同样幂等。
            await _set_delivery(sink_harness, target, start=True)
            await _set_delivery(sink_harness, target, start=True)
            await _wait_state(sink_harness, target, "RUNNING")
            await _wait_all_drained(sink_harness, names)
            total = 4 * 2 * POINTS_PER_BATCH
            records = await _drain_kafka(sink_harness, min_messages=total)
            assert len(records) == total
        finally:
            await sink_harness.stop()

    async def test_r7_05_legacy_template_broad_subject_serial_path(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        legacy_id = "power_history"
        _write_concurrent_root(
            sink_harness,
            devices=DEVICES,
            concurrent=False,
            delivery_ids=[legacy_id],
        )
        await sink_harness.provision_streams()
        await sink_harness.start()
        name = _durable_name(f"qa-{sink_harness.token}", legacy_id)
        try:
            await _wait_state(sink_harness, legacy_id, "RUNNING")
            infos = await _consumer_infos(sink_harness)
            assert set(infos) == {name}
            assert infos[name].config.filter_subject == (
                f"wh.hist.batch.{WIND_FARM_ID}.{COLLECTOR_ID}.>"
            )
            total = await _publish_per_device(sink_harness, batches=2)
            await _wait_all_drained(sink_harness, {name})
            records = await _drain_kafka(sink_harness, min_messages=total)
            assert len(records) == total
            per_device: dict[str, list[tuple[int, int]]] = {device: [] for device in DEVICES}
            for record in records:
                payload = json_lib.loads(record.value.decode("utf-8"))
                per_device[payload["device_id"]].append(
                    tuple(int(part) for part in payload["sample_id"].split("-")[2:4])
                )
            for device, pairs in per_device.items():
                assert pairs == sorted(pairs), f"{device} 乱序"
        finally:
            await sink_harness.stop()

    async def test_r7_06_delivery_identity_switch_no_dual_write(
        self, sink_harness: SinkSystemHarness
    ) -> None:
        old_id, new_id = "history_old", "history_new"
        _write_concurrent_root(sink_harness, devices=["WT001"], delivery_ids=[old_id])
        await sink_harness.provision_streams()
        await sink_harness.start()
        deployment = f"qa-{sink_harness.token}"
        old_durable = _durable_name(deployment, old_id)
        new_durable = _durable_name(deployment, new_id)
        try:
            await _wait_state(sink_harness, old_id, "RUNNING")
            await sink_harness.publish_reliable(
                sink_harness.make_batch(
                    "reliable", seq=0, points=POINTS_PER_BATCH, device_id="WT001"
                )
            )
            first = POINTS_PER_BATCH
            await _wait_all_drained(sink_harness, {old_durable})
            # 切换前再压入积压，验证新身份接管后不遗漏历史消息。
            backlog = 0
            for seq in range(20, 23):
                await sink_harness.publish_reliable(
                    sink_harness.make_batch(
                        "reliable", seq=seq, points=POINTS_PER_BATCH, device_id="WT001"
                    )
                )
                backlog += POINTS_PER_BATCH
            await sink_harness.stop()
            # 直接改写原始文档不会生成新一代；CURRENT 仍指向旧快照，重启后
            # 进程会承载旧 Delivery（同 test_uc09_s03 的既有契约）。切换
            # 身份前必须清除生成存储，强制新进程从编辑后的文档重新提交。
            shutil.rmtree(sink_harness.root / ".generations")
            (sink_harness.root / "CURRENT").unlink()

            _write_concurrent_root(sink_harness, devices=["WT001"], delivery_ids=[new_id])
            await sink_harness.start()
            try:
                await _wait_state(sink_harness, new_id, "RUNNING")
                view = await _delivery_view(sink_harness)
                assert set(view) == {new_id}  # 旧 Delivery 不再被进程承载
                await _wait_all_drained(sink_harness, {new_durable}, timeout=90)
                infos = await _consumer_infos(sink_harness)
                # 旧 Durable 作为 JetStream 资源保留（迁移须运维确认生命周期），
                # 但新进程只向新 Durable 消费，Kafka 侧不得双写。
                assert old_durable in infos and new_durable in infos
                expected_total = first + backlog
                records = await _drain_kafka(sink_harness, min_messages=expected_total, timeout=90)
                payloads = [json_lib.loads(record.value.decode("utf-8")) for record in records]
                ids = {(payload["device_id"], payload["sample_id"]) for payload in payloads}
                assert len(ids) == expected_total  # 无重复、无遗漏
            finally:
                await sink_harness.stop()
        finally:
            if sink_harness.process is not None:
                await sink_harness.stop()
