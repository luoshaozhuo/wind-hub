"""R1 P1-01：真实 NATS JetStream 下多 Delivery 独立 Durable ACK 责任。

生产 ``ReliableDeliveryPort`` 对接真实 ``nats-server``（JetStream file 存储），
目标 Sink 使用测试内 FakePool/FakeKafkaAdapter 接缝注入成功/失败，**Broker 侧
观察全部走独立 nats 连接的 ConsumerInfo/Stream State/DLQ 记录**，不检查 Mock
调用次数。覆盖任务书 P1-01 的：

- A 成功 B UNKNOWN：A 独立 ACK，B 保持未确认并重投，转正后各自结清；
- 双成功：各自 ACK，不存在跨 Durable 全局确认；
- 永久非法批次：DLQ PubAck 后才 ACK；DLQ 不可用时源消息保留未确认责任；
- STOP/重启：同一 Durable 名重绑，未确认责任不与已确认 Durable 混淆；
- Subject 与消息身份不一致：进 DLQ，不得按普通零匹配直接 ACK；
- 合法来源零匹配：按当前 Delivery 权威配置安全跳过并 ACK；
- 多 Delivery 共享进程/池时 ACK、统计与资源额度互不混用。

环境变量 ``WINDHUB_NATS_SERVER_BIN`` 指向 nats-server 可执行文件；缺失时
整个模块 skip（NOT_RUN 口径，绝不退化为内存替身）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nats
import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.config import validate_snapshot
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import BatchOutcome
from sink_service.domain.config import ValidatedSnapshot
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryPort
from tests.sink_service._stage1_fixtures import (
    SERVICE_FILE,
    snapshot_docs,
    snapshot_files,
)

pytestmark = [pytest.mark.integration, pytest.mark.reliability, pytest.mark.real_service]

FARM = "farm-a"
COLLECTOR = "collector-1"
DEVICE = "WT001"
SUBJECT_PREFIX = "wh.hist.batch"
SOURCE_STREAM = "WH_HISTORY"
DLQ_STREAM = "WH_DLQ"
DLQ_SUBJECT = "wh.dlq"

SERVER_BIN = os.environ.get("WINDHUB_NATS_SERVER_BIN", "")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def broker(tmp_path: Path) -> AsyncIterator[str]:
    """每个用例独立 nats-server 进程与 file 存储，保证 Consumer/Stream 全隔离。"""
    if not SERVER_BIN or not Path(SERVER_BIN).is_file():
        pytest.skip("WINDHUB_NATS_SERVER_BIN not set; real JetStream verification NOT_RUN")
    store = tmp_path / "nats-store"
    port = _free_port()
    proc = subprocess.Popen(
        [
            SERVER_BIN,
            "-js",
            "-sd",
            str(store),
            "-p",
            str(port),
            # 测试只走 loopback，关闭外部探测端点。
            "-m",
            "-1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    while loop.time() < deadline:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.5
            )
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionError, OSError, TimeoutError):
            if proc.poll() is not None:
                raise RuntimeError("nats-server exited early") from None
            await asyncio.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("nats-server did not open its client port")
    url = f"nats://127.0.0.1:{port}"
    client = await _recreate_streams(url)
    try:
        yield url
    finally:
        await client.drain()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


async def _client(nats_url: str):
    return await nats.connect(servers=nats_url, connect_timeout=10)


async def _recreate_streams(nats_url: str) -> Any:
    from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

    client = await _client(nats_url)
    js = client.jetstream()
    for name in (SOURCE_STREAM, DLQ_STREAM):
        with contextlib.suppress(Exception):
            await js.delete_stream(name)
    await js.add_stream(
        StreamConfig(
            name=SOURCE_STREAM,
            subjects=[f"{SUBJECT_PREFIX}.>"],
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.NEW,
            max_bytes=16 * 1024 * 1024,
            max_age=7 * 24 * 3600,
        )
    )
    await js.add_stream(
        StreamConfig(
            name=DLQ_STREAM,
            subjects=[DLQ_SUBJECT],
            storage=StorageType.FILE,
            retention=RetentionPolicy.LIMITS,
            discard=DiscardPolicy.NEW,
            max_bytes=1024 * 1024 * 1024,
            max_age=86400,
        )
    )
    return client


async def _drop_dlq(nats_url: str) -> None:
    client = await _client(nats_url)
    try:
        await client.jetstream().delete_stream(DLQ_STREAM)
    finally:
        await client.drain()


async def _recreate_dlq(nats_url: str) -> None:
    from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

    client = await _client(nats_url)
    try:
        js = client.jetstream()
        await js.add_stream(
            StreamConfig(
                name=DLQ_STREAM,
                subjects=[DLQ_SUBJECT],
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.NEW,
                max_bytes=1024 * 1024 * 1024,
                max_age=86400,
            )
        )
    finally:
        await client.drain()


def _delivery_instance(*, sink: str, devices: list[str], points: list[str]) -> dict[str, Any]:
    return {
        "template_id": "reliable_history",
        "template_version": "1.0.0",
        "sink_id": sink,
        "desired_state": "START",
        "source": {
            "wind_farm_id": FARM,
            "collector_id": COLLECTOR,
            "devices": devices,
            "points": points,
        },
        "overrides": {},
    }


def _kafka_sink(port: int = 9092) -> dict[str, Any]:
    # FakePool 从不建立真实 Kafka 连接；不同 bootstrap 仅为通过生产校验器的
    # 目标地址唯一性（同地址禁止跨 Delivery 点重叠）。
    return {
        "template_id": "kafka_history",
        "template_version": "1.0.0",
        "connection": {"bootstrap_servers": [f"127.0.0.1:{port}"]},
        "overrides": {},
    }


def _snapshot(
    nats_url: str,
    *,
    deployment_id: str,
    deliveries: dict[str, dict[str, Any]],
    sinks: dict[str, dict[str, Any]],
) -> ValidatedSnapshot:
    docs = snapshot_docs(
        deliveries=deliveries,
        sinks=sinks,
        include_templates=("kafka_history", "reliable_history"),
    )
    service = docs[SERVICE_FILE]
    service["deployment_id"] = deployment_id
    service["nats"]["servers"] = [nats_url]
    service["reliable"]["ack_wait_seconds"] = 1
    service["reliable"]["retry_base_ms"] = 200
    service["reliable"]["retry_max_ms"] = 1000
    return validate_snapshot(snapshot_files(docs))


class FakeKafkaAdapter:
    """write_encoded_batch 接缝：mode 切换成功/瞬断，记录调用次数与在途状态。"""

    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.calls = 0
        self._inflight = False

    @property
    def io_inflight(self) -> bool:
        return self._inflight

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def settle(self) -> None:
        return None

    async def write_encoded_batch(
        self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
    ) -> BatchOutcome:
        self.calls += 1
        self._inflight = True
        try:
            count = len(payloads)
            if self.mode == "success":
                return BatchOutcome(
                    delivered=tuple(range(count)),
                    settled=tuple(range(count)),
                    successful_attempts=1,
                )
            return BatchOutcome(
                delivered=(),
                settled=tuple(range(count)),
                error="injected transient target outage",
                failed_attempts=1,
            )
        finally:
            self._inflight = False


class FakePool:
    """每个 sink 一个独立适配器；不发起任何真实 Kafka 连接。"""

    def __init__(self, adapters: dict[str, FakeKafkaAdapter]) -> None:
        self._adapters = adapters
        self.releases: list[str] = []

    async def acquire(self, sink_id: str, snapshot: ValidatedSnapshot) -> FakeKafkaAdapter:
        return self._adapters[sink_id]

    async def release(self, sink_id: str) -> None:
        self.releases.append(sink_id)


def _port(
    snapshot: ValidatedSnapshot, pool: FakePool, delivery_id: str, sink_id: str
) -> ReliableDeliveryPort:
    definition = DeliveryDefinition(
        delivery_id=delivery_id,
        sink_id=sink_id,
        channel="reliable",
        desired_state="START",
    )
    return ReliableDeliveryPort(definition, snapshot, pool)


def _durable(snapshot: ValidatedSnapshot, delivery_id: str) -> str:
    return ReliableDeliveryPort._durable_id(snapshot, delivery_id)


def make_batch(*, device: str = DEVICE, point: str = "active_power") -> SampleBatch:
    now = datetime.now(UTC)
    return SampleBatch(
        batch_id=f"b-{uuid.uuid4()}",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id="task-qa",
        device_id=device,
        publish_time=now,
        samples=(
            SampleItem(
                point_id=point,
                sample_id=f"s-{uuid.uuid4()}",
                value=1.5,
                quality="good",
                sample_time=now,
            ),
        ),
    )


async def _publish_batch(nats_url: str, batch: SampleBatch) -> None:
    client = await _client(nats_url)
    try:
        await client.jetstream().publish(batch.subject(), batch.encode(), timeout=10)
    finally:
        await client.drain()


async def _publish_raw(nats_url: str, subject: str, payload: bytes) -> None:
    client = await _client(nats_url)
    try:
        await client.jetstream().publish(subject, payload, timeout=10)
    finally:
        await client.drain()


async def _consumer_info(nats_url: str, stream: str, durable: str) -> Any:
    client = await _client(nats_url)
    try:
        return await client.jetstream().consumer_info(stream, durable)
    finally:
        await client.drain()


async def _wait_for(
    condition, *, timeout: float = 20.0, interval: float = 0.2, label: str = "condition"
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await condition():
                return
        except Exception as exc:  # noqa: BLE001 - 探测期 Broker 瞬态可读
            last = exc
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out waiting for {label}: {last}")


async def _stream_messages(nats_url: str, stream: str) -> int:
    client = await _client(nats_url)
    try:
        return (await client.jetstream().stream_info(stream)).state.messages
    finally:
        await client.drain()


async def _dlq_records(nats_url: str) -> list[dict[str, Any]]:
    import json

    client = await _client(nats_url)
    try:
        js = client.jetstream()
        sub = await js.pull_subscribe(DLQ_SUBJECT, durable=None, stream=DLQ_STREAM)
        records: list[dict[str, Any]] = []
        while True:
            try:
                messages = await sub.fetch(batch=100, timeout=1.0)
            except Exception:
                break
            for message in messages:
                records.append(json.loads(message.data.decode()))
                await message.ack()
        return records
    finally:
        await client.drain()


async def test_independent_ack_when_one_delivery_succeeds_and_other_unknowns(broker) -> None:
    nats_url = broker
    adapters = {"sink_a": FakeKafkaAdapter(), "sink_b": FakeKafkaAdapter(mode="fail")}
    pool = FakePool(adapters)
    deliveries = {
        "da": _delivery_instance(sink="sink_a", devices=[DEVICE], points=["active_power"]),
        "db": _delivery_instance(sink="sink_b", devices=[DEVICE], points=["active_power"]),
    }
    snapshot = _snapshot(
        nats_url,
        deployment_id=f"dep-case1-{uuid.uuid4().hex[:8]}",
        deliveries=deliveries,
        sinks={"sink_a": _kafka_sink(19092), "sink_b": _kafka_sink(19093)},
    )
    port_a = _port(snapshot, pool, "da", "sink_a")
    port_b = _port(snapshot, pool, "db", "sink_b")
    durable_a, durable_b = _durable(snapshot, "da"), _durable(snapshot, "db")
    await port_a.start()
    await port_b.start()
    try:
        await _publish_batch(nats_url, make_batch())

        # A 成功：立即独立 ACK，零重投。
        await _wait_for(
            lambda: _settled(nats_url, SOURCE_STREAM, durable_a),
            label="durable A acknowledged",
        )
        info_a = await _consumer_info(nats_url, SOURCE_STREAM, durable_a)
        assert info_a.num_redelivered == 0
        assert port_a.acknowledged_batches_total == 1
        assert port_b.acknowledged_batches_total == 0

        # B UNKNOWN：NAK 后 Broker 持续重投，源消息因 B 未确认而保留。
        await _wait_for(
            lambda: _redelivered_at_least(nats_url, SOURCE_STREAM, durable_b, 2),
            label="durable B redelivered twice",
        )
        info_b = await _consumer_info(nats_url, SOURCE_STREAM, durable_b)
        assert info_b.num_ack_pending == 1
        # A 已结清而 B 仍持有未确认责任：A 的 ACK 不消除 B 的责任。
        assert await _settled(nats_url, SOURCE_STREAM, durable_a)
        assert not await _settled(nats_url, SOURCE_STREAM, durable_b)

        # B 恢复后自行结清；A 的 ACK 从未替代 B 的责任。
        adapters["sink_b"].mode = "success"
        await _wait_for(
            lambda: _settled(nats_url, SOURCE_STREAM, durable_b),
            label="durable B acknowledged after recovery",
        )
        # Limits 保留策略下 ACK 只推进 Consumer、不删 Stream 消息；两个
        # Durable 的 ACK 责任均以各自 Consumer pending 归零为权威证据。
        assert await _settled(nats_url, SOURCE_STREAM, durable_a)
        assert await _settled(nats_url, SOURCE_STREAM, durable_b)
        assert port_b.acknowledged_batches_total == 1
        # 统计互不混用：A 从未执行 B 的写入，反之亦然。
        assert adapters["sink_a"].calls == 1 and adapters["sink_b"].calls >= 3
        assert port_a.delivered_total == 1 and port_b.delivered_total == 1
    finally:
        await port_b.stop()
        await port_a.stop()


async def test_both_success_ack_independently_with_shared_pool(broker) -> None:
    nats_url = broker
    adapters = {"sink_a": FakeKafkaAdapter(), "sink_b": FakeKafkaAdapter()}
    pool = FakePool(adapters)
    deliveries = {
        "da": _delivery_instance(sink="sink_a", devices=[DEVICE], points=["active_power"]),
        "db": _delivery_instance(sink="sink_b", devices=[DEVICE], points=["active_power"]),
    }
    snapshot = _snapshot(
        nats_url,
        deployment_id=f"dep-case2-{uuid.uuid4().hex[:8]}",
        deliveries=deliveries,
        sinks={"sink_a": _kafka_sink(19092), "sink_b": _kafka_sink(19093)},
    )
    port_a = _port(snapshot, pool, "da", "sink_a")
    port_b = _port(snapshot, pool, "db", "sink_b")
    durable_a, durable_b = _durable(snapshot, "da"), _durable(snapshot, "db")
    await port_a.start()
    await port_b.start()
    try:
        await _publish_batch(nats_url, make_batch())
        await _wait_for(
            lambda: _both_settled(nats_url, durable_a, durable_b),
            label="both Durables acknowledged independently",
        )
        info_a = await _consumer_info(nats_url, SOURCE_STREAM, durable_a)
        info_b = await _consumer_info(nats_url, SOURCE_STREAM, durable_b)
        assert info_a.num_redelivered == 0 and info_b.num_redelivered == 0
        assert await _settled(nats_url, SOURCE_STREAM, durable_a)
        assert await _settled(nats_url, SOURCE_STREAM, durable_b)
        # 两个 Durable 名称不同，各自只 ACK 自己的 Consumer。
        assert durable_a != durable_b
        assert port_a.acknowledged_batches_total == port_b.acknowledged_batches_total == 1
        assert port_a.retry_total == port_b.retry_total == 0
    finally:
        await port_b.stop()
        await port_a.stop()
    # 共享池在 STOP 时分别释放各自 sink，额度不串用。
    assert sorted(pool.releases) == ["sink_a", "sink_b"]


async def test_invalid_batch_requires_dlq_puback_before_durable_ack(broker) -> None:
    nats_url = broker
    adapters = {"sink_a": FakeKafkaAdapter(), "sink_b": FakeKafkaAdapter()}
    pool = FakePool(adapters)
    deliveries = {
        "da": _delivery_instance(sink="sink_a", devices=[DEVICE], points=["active_power"]),
        "db": _delivery_instance(sink="sink_b", devices=[DEVICE], points=["active_power"]),
    }
    snapshot = _snapshot(
        nats_url,
        deployment_id=f"dep-case3-{uuid.uuid4().hex[:8]}",
        deliveries=deliveries,
        sinks={"sink_a": _kafka_sink(19092), "sink_b": _kafka_sink(19093)},
    )
    port_a = _port(snapshot, pool, "da", "sink_a")
    port_b = _port(snapshot, pool, "db", "sink_b")
    durable_a, durable_b = _durable(snapshot, "da"), _durable(snapshot, "db")
    await port_a.start()
    await port_b.start()
    try:
        subject = f"{SUBJECT_PREFIX}.{FARM}.{COLLECTOR}.{DEVICE}"
        await _publish_raw(nats_url, subject, b"{this is not valid json")
        await _wait_for(
            lambda: _both_settled(nats_url, durable_a, durable_b),
            label="both Durables ACK after DLQ PubAck",
        )
        # 源消息仅在 DLQ PubAck 之后才允许 ACK：两 Consumer 均结清，
        # 且 DLQ Stream 中各 Durable 的持久证据齐备。
        records = await _dlq_records(nats_url)
        # 每个 Durable 独立写自己的 DLQ 证据：两条记录、两个 consumer/delivery。
        assert len(records) == 2
        assert {record["delivery_id"] for record in records} == {"da", "db"}
        assert {record["classification"] for record in records} == {"PERMANENT"}
        assert all(record["subject"] == subject for record in records)
        assert all(record["reason"] for record in records)
        # 非法批次不触碰目标 Sink。
        assert adapters["sink_a"].calls == adapters["sink_b"].calls == 0
        assert port_a.dlq_invalid_batches_total == port_b.dlq_invalid_batches_total == 1
    finally:
        await port_b.stop()
        await port_a.stop()


async def test_dlq_unavailable_retains_source_responsibility_then_recovers(broker) -> None:
    nats_url = broker
    adapter = FakeKafkaAdapter()
    pool = FakePool({"sink_z": adapter})
    # devices=[] → 通配 filter，能收同 collector 下任意设备 subject。
    deliveries = {
        "dz": _delivery_instance(sink="sink_z", devices=[], points=["active_power"]),
    }
    snapshot = _snapshot(
        nats_url,
        deployment_id=f"dep-case4-{uuid.uuid4().hex[:8]}",
        deliveries=deliveries,
        sinks={"sink_z": _kafka_sink()},
    )
    durable_z = _durable(snapshot, "dz")
    port = _port(snapshot, pool, "dz", "sink_z")
    # 生产 start() 强制校验 DLQ Stream 存在，故先正常启动，再在运行中摘除
    # DLQ，模拟真实故障窗口而非非法启动配置。
    await port.start()
    try:
        await _drop_dlq(nats_url)
        subject = f"{SUBJECT_PREFIX}.{FARM}.{COLLECTOR}.{DEVICE}"
        await _publish_raw(nats_url, subject, b"{broken")
        # DLQ 不可用：源消息持续未确认、反复重投，绝不伪 ACK。
        await _wait_for(
            lambda: _redelivered_at_least(nats_url, SOURCE_STREAM, durable_z, 2),
            label="redelivery while DLQ unavailable",
        )
        info = await _consumer_info(nats_url, SOURCE_STREAM, durable_z)
        assert info.num_ack_pending == 1
        assert port.acknowledged_batches_total == 0
        assert "DLQ_BLOCKED" in port.last_error

        # DLQ 恢复：同一条消息走 DLQ 后 ACK，责任闭环。
        await _recreate_dlq(nats_url)
        await _wait_for(
            lambda: _settled(nats_url, SOURCE_STREAM, durable_z),
            label="ACK after DLQ recovery",
        )
        records = await _dlq_records(nats_url)
        assert len(records) == 1 and records[0]["delivery_id"] == "dz"
    finally:
        await port.stop()


async def test_stop_and_restart_keeps_unacked_responsibility_distinct(broker) -> None:
    nats_url = broker
    fail_adapter = FakeKafkaAdapter(mode="fail")
    ok_adapter = FakeKafkaAdapter()
    deliveries = {
        "db": _delivery_instance(sink="sink_b", devices=[DEVICE], points=["active_power"]),
    }
    snapshot = _snapshot(
        nats_url,
        deployment_id=f"dep-case5-{uuid.uuid4().hex[:8]}",
        deliveries=deliveries,
        sinks={"sink_b": _kafka_sink()},
    )
    durable_b = _durable(snapshot, "db")
    first_pool = FakePool({"sink_b": fail_adapter})
    port = _port(snapshot, first_pool, "db", "sink_b")
    await port.start()
    try:
        await _publish_batch(nats_url, make_batch())
        await _wait_for(
            lambda: _redelivered_at_least(nats_url, SOURCE_STREAM, durable_b, 1),
            label="redelivery before STOP",
        )
    finally:
        await port.stop()

    # 重启：同一 Durable 名绑定同一持久 Consumer，未确认消息重新投递，
    # 不依赖进程内状态，也不会与其他 Durable 的 ACK 混淆。
    second_pool = FakePool({"sink_b": ok_adapter})
    restarted = _port(snapshot, second_pool, "db", "sink_b")
    await restarted.start()
    try:
        await _wait_for(
            lambda: _settled(nats_url, SOURCE_STREAM, durable_b),
            label="restarted Durable drains inherited responsibility",
        )
        info = await _consumer_info(nats_url, SOURCE_STREAM, durable_b)
        assert info.num_pending == 0 and info.num_ack_pending == 0
        assert restarted.acknowledged_batches_total == 1
        assert ok_adapter.calls == 1
    finally:
        await restarted.stop()


async def test_subject_identity_mismatch_goes_to_dlq_not_zero_match_ack(broker) -> None:
    nats_url = broker
    adapters = {"sink_a": FakeKafkaAdapter(), "sink_b": FakeKafkaAdapter()}
    pool = FakePool(adapters)
    deliveries = {
        "da": _delivery_instance(sink="sink_a", devices=[DEVICE], points=["active_power"]),
        "db": _delivery_instance(sink="sink_b", devices=[DEVICE], points=["active_power"]),
    }
    snapshot = _snapshot(
        nats_url,
        deployment_id=f"dep-case6-{uuid.uuid4().hex[:8]}",
        deliveries=deliveries,
        sinks={"sink_a": _kafka_sink(19092), "sink_b": _kafka_sink(19093)},
    )
    port_a = _port(snapshot, pool, "da", "sink_a")
    port_b = _port(snapshot, pool, "db", "sink_b")
    durable_a, durable_b = _durable(snapshot, "da"), _durable(snapshot, "db")
    await port_a.start()
    await port_b.start()
    try:
        # 消息体自称 WT002，却发布到 WT001 subject：decode 身份校验失败。
        mismatched = make_batch(device="WT002")
        wrong_subject = f"{SUBJECT_PREFIX}.{FARM}.{COLLECTOR}.{DEVICE}"
        await _publish_raw(nats_url, wrong_subject, mismatched.encode())
        await _wait_for(
            lambda: _both_settled(nats_url, durable_a, durable_b),
            label="mismatched message DLQ-acked by both Durables",
        )
        records = await _dlq_records(nats_url)
        assert len(records) == 2
        assert all(record["subject"] == wrong_subject for record in records)
        assert all("subject" in record["reason"].lower() for record in records)
        # 绝不能作为普通零匹配跳过：目标 Sink 无写入，证据在 DLQ。
        assert adapters["sink_a"].calls == adapters["sink_b"].calls == 0
    finally:
        await port_b.stop()
        await port_a.stop()


async def test_legitimate_zero_match_safely_skips_without_dlq(broker) -> None:
    nats_url = broker
    adapter = FakeKafkaAdapter()
    pool = FakePool({"sink_z": adapter})
    # 通配 filter 收消息，但权威点表不含批次中的点 → 合法零匹配。
    deliveries = {
        "dz": _delivery_instance(sink="sink_z", devices=[], points=["other_point"]),
    }
    snapshot = _snapshot(
        nats_url,
        deployment_id=f"dep-case7-{uuid.uuid4().hex[:8]}",
        deliveries=deliveries,
        sinks={"sink_z": _kafka_sink()},
    )
    durable_z = _durable(snapshot, "dz")
    port = _port(snapshot, pool, "dz", "sink_z")
    await port.start()
    try:
        await _publish_batch(nats_url, make_batch())
        await _wait_for(
            lambda: _settled(nats_url, SOURCE_STREAM, durable_z),
            label="zero-match batch safely skipped and ACKed",
        )
        assert adapter.calls == 0
        assert port.dlq_total == 0 and port.dlq_invalid_batches_total == 0
        assert port.acknowledged_batches_total == 1
        assert await _stream_messages(nats_url, DLQ_STREAM) == 0
    finally:
        await port.stop()


# ---- Broker 侧判定原语（独立连接，不读进程内状态）-----------------------


async def _settled(nats_url: str, stream: str, durable: str) -> bool:
    info = await _consumer_info(nats_url, stream, durable)
    return info.num_pending == 0 and info.num_ack_pending == 0


async def _redelivered_at_least(
    nats_url: str, stream: str, durable: str, redeliveries: int
) -> bool:
    # nats-server 的 num_redelivered 不累计（恒 0/1，ACK 后归零）；
    # delivered.consumer_seq 是 Broker 侧权威投递序号：首次为 1，
    # 每次 NAK 重投递增，故“至少重投 N 次”等价于 consumer_seq >= 1+N。
    info = await _consumer_info(nats_url, stream, durable)
    return info.delivered.consumer_seq >= 1 + redeliveries


async def _both_settled(nats_url: str, durable_a: str, durable_b: str) -> bool:
    return await _settled(nats_url, SOURCE_STREAM, durable_a) and await _settled(
        nats_url, SOURCE_STREAM, durable_b
    )
