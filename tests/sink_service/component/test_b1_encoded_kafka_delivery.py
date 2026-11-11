"""B1 预编码 Reliable Kafka 路径的组件级测试（fake adapter，无 Docker）。

钉死生产化 B1 的分派与记账契约：

- Kafka Reliable 完全跳过逐点 DeliveryPoint 构造，只调
  write_encoded_batch；非 Kafka 目标只调旧 write_batch，且旧 adapter
  不需要/不暴露预编码接口；
- 500 点边界与超 500 点拆批、子批间 InProgress 续租、选择器筛选后的
  索引与 received/delivered 记账；
- 预编码 payload 与生产 DeliveryPoint.encode 逐字节相等，key/stripe
  与 KafkaOutput 的设备锁语义一致；
- UNKNOWN settle、permanent 拒绝、adapter 失败均不伪造成功。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.config import validate_snapshot
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_point import select_delivery_points
from sink_service.application.realtime import BatchOutcome
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryPort
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    snapshot_docs,
    snapshot_files,
)

pytestmark = [pytest.mark.component, pytest.mark.reliability]

FARM = "farm-a"
COLLECTOR = "collector-1"
DEVICE = "WT001"
DEFINITION = DeliveryDefinition("power_history", "history_kafka", "reliable", "START")


class _FakeJetStreamMessage:
    def __init__(self) -> None:
        self.in_progress_calls = 0

    async def in_progress(self) -> None:
        self.in_progress_calls += 1


class _RecordingAdapter:
    """同时实现两个接口，记录调用参数并可注入结果。"""

    def __init__(self, outcome: BatchOutcome | None = None) -> None:
        self.encoded_calls: list[tuple[tuple[bytes, ...], bytes, int]] = []
        self.legacy_calls: list[tuple[Any, ...]] = []
        self.settle_calls = 0
        self._outcome = outcome

    async def write_encoded_batch(
        self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
    ) -> BatchOutcome:
        self.encoded_calls.append((payloads, key, stripe))
        if self._outcome is not None:
            return self._outcome
        return BatchOutcome(
            delivered=tuple(range(len(payloads))),
            settled=tuple(range(len(payloads))),
            successful_attempts=len(payloads),
            successful_latencies_ms=tuple(1.0 for _ in payloads),
        )

    async def write_batch(self, samples: tuple[Any, ...]) -> BatchOutcome:
        self.legacy_calls.append(samples)
        return BatchOutcome(
            delivered=tuple(range(len(samples))),
            settled=tuple(range(len(samples))),
            successful_attempts=len(samples),
            successful_latencies_ms=tuple(1.0 for _ in samples),
        )

    async def settle(self) -> BatchOutcome | None:
        self.settle_calls += 1
        return None


class _LegacyOnlyAdapter:
    """非 Kafka adapter：没有 write_encoded_batch，误调会直接 AttributeError。"""

    def __init__(self) -> None:
        self.legacy_calls = 0

    async def write_batch(self, samples: tuple[Any, ...]) -> BatchOutcome:
        self.legacy_calls += 1
        return BatchOutcome(
            delivered=tuple(range(len(samples))),
            settled=tuple(range(len(samples))),
            successful_attempts=len(samples),
        )


def _docs(*, points: list[str] | None = None) -> dict[str, dict]:
    docs = snapshot_docs(include_templates=("reliable_history", "kafka_history", "redis_realtime"))
    docs[DELIVERIES_INSTANCE]["deliveries"] = {
        "power_history": {
            "template_id": "reliable_history",
            "template_version": "1.0.0",
            "sink_id": "history_kafka",
            "desired_state": "START",
            "source": {
                "wind_farm_id": FARM,
                "collector_id": COLLECTOR,
                "devices": [DEVICE],
                # 空选择器表示全测点，与生产语义一致（config.py 已钉死）
                "points": [] if points is None else points,
            },
            "overrides": {},
        }
    }
    return docs


def _port(*, kafka_target: bool, adapter: Any) -> ReliableDeliveryPort:
    snapshot = validate_snapshot(snapshot_files(_docs()))
    port = ReliableDeliveryPort(DEFINITION, snapshot, pool=object())
    port._adapter = adapter
    port._kafka_target = kafka_target
    return port


def _batch(points: int) -> SampleBatch:
    now = datetime.now(UTC)
    return SampleBatch(
        batch_id="b-r9",
        mode="reliable",
        wind_farm_id=FARM,
        collector_id=COLLECTOR,
        task_id="task-r9",
        device_id=DEVICE,
        publish_time=now,
        samples=tuple(
            SampleItem(
                point_id=f"p{idx:03d}",
                sample_id=f"s-r9-{idx}",
                value=float(idx),
                quality="good",
                sample_time=now,
                max_staleness_ms=None,
            )
            for idx in range(points)
        ),
    )


def test_kafka_target_uses_only_preencoded_interface_with_byte_identical_payloads() -> None:
    adapter = _RecordingAdapter()
    port = _port(kafka_target=True, adapter=adapter)
    batch = _batch(500)
    message = _FakeJetStreamMessage()

    assert asyncio.run(port._deliver_batch(message, batch)) is True

    assert len(adapter.encoded_calls) == 1
    payloads, key, stripe = adapter.encoded_calls[0]
    assert adapter.legacy_calls == []
    assert message.in_progress_calls == 0  # 单批无需续租
    assert key == f"{FARM}:{COLLECTOR}:{DEVICE}".encode()
    assert stripe == hash((FARM, COLLECTOR, DEVICE)) % 128
    # 与权威 DeliveryPoint 编码逐字节相等
    legacy = select_delivery_points(batch)
    assert len(payloads) == 500
    assert all(payloads[i] == legacy[i].encode() for i in range(500))
    assert port.received_total == 500
    assert port.delivered_total == 500
    assert port.write_success_total == 500


def test_more_than_500_points_split_with_heartbeat_between_subbatches() -> None:
    adapter = _RecordingAdapter()
    port = _port(kafka_target=True, adapter=adapter)
    # SampleBatch 契约上限 1024 点；1024 仍可验证 500 边界三段拆分
    batch = _batch(1024)
    message = _FakeJetStreamMessage()

    assert asyncio.run(port._deliver_batch(message, batch)) is True

    assert [len(call[0]) for call in adapter.encoded_calls] == [500, 500, 24]
    assert message.in_progress_calls == 2  # 仅子批之间续租
    assert port.received_total == 1024
    assert port.delivered_total == 1024
    assert port.write_success_total == 1024
    # 第二批首个 payload 仍是正确的第 500 点（子批索引不错位）
    assert adapter.encoded_calls[1][0][0] == select_delivery_points(batch)[500].encode()
    assert adapter.encoded_calls[2][0][-1] == select_delivery_points(batch)[1023].encode()


def test_non_kafka_target_never_calls_preencoded_interface() -> None:
    adapter = _LegacyOnlyAdapter()
    port = _port(kafka_target=False, adapter=adapter)

    assert asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(500))) is True
    assert adapter.legacy_calls == 1
    assert port.received_total == 500
    assert port.delivered_total == 500


def test_point_selector_filters_before_encoding_and_accounting() -> None:
    snapshot = validate_snapshot(snapshot_files(_docs(points=["p001", "p002"])))
    adapter = _RecordingAdapter()
    port = ReliableDeliveryPort(DEFINITION, snapshot, pool=object())
    port._adapter = adapter
    port._kafka_target = True

    batch = _batch(10)
    assert asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), batch)) is True
    payloads, _key, _stripe = adapter.encoded_calls[0]
    assert len(payloads) == 2
    legacy = select_delivery_points(batch, frozenset({"p001", "p002"}))
    assert list(payloads) == [sample.encode() for sample in legacy]
    assert port.received_total == 2
    assert port.delivered_total == 2


def test_adapter_failure_is_not_reported_as_success() -> None:
    failure = BatchOutcome((), (), "Kafka sample 2 not queued: boom")
    adapter = _RecordingAdapter(outcome=failure)
    port = _port(kafka_target=True, adapter=adapter)

    assert asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(5))) is False
    assert port.delivered_total == 0
    assert port.write_success_total == 0


def test_kafka_target_rejects_permanent_point_classification() -> None:
    outcome = BatchOutcome(
        delivered=(0,),
        settled=(0, 1),
        error="",
        successful_attempts=1,
        permanent=(1,),
    )
    adapter = _RecordingAdapter(outcome=outcome)
    port = _port(kafka_target=True, adapter=adapter)

    with pytest.raises(RuntimeError, match="permanent point failures"):
        asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(2)))


def test_unknown_outcome_falls_back_to_settle() -> None:
    class _UncertainAdapter(_RecordingAdapter):
        async def write_encoded_batch(
            self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
        ) -> BatchOutcome:
            self.encoded_calls.append((payloads, key, stripe))
            return BatchOutcome((), (), "Sink I/O result uncertain; waiting for settlement")

    adapter = _UncertainAdapter()
    port = _port(kafka_target=True, adapter=adapter)
    # settle() 返回 None 时保留原 UNKNOWN 结果，不得记成功
    assert asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(3))) is False
    assert adapter.settle_calls == 1
    assert port.delivered_total == 0
