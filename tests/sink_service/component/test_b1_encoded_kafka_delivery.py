"""B1 预编码 Reliable Kafka 路径的组件级测试（fake adapter，无 Docker）。

钉死生产化 B1 的分派与记账契约：

- Kafka Reliable 完全跳过逐点 DeliveryPoint 构造，只调
  write_encoded_batch；非 Kafka 目标只调旧 write_batch，且旧 adapter
  不需要/不暴露预编码接口；
- 500/1024 点设备行必须一次输出且严格拒绝不完整选择；
- v3 预编码 payload 与正式 Outbound Kafka Adapter 编码逐字节相等，key/stripe
  与 KafkaOutput 的设备锁语义一致；
- UNKNOWN settle、permanent 拒绝、adapter 失败均不伪造成功。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from zlib import crc32

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import BatchOutcome
from sink_service.infrastructure.adapters.outbound.kafka_wire import encode_kafka_wide
from sink_service.infrastructure.config_io import validate_snapshot
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryRuntime
from storage_service.domain.wide_record import WideRecord
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

    async def extend(self) -> None:
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


def _port(*, kafka_target: bool, adapter: Any) -> ReliableDeliveryRuntime:
    snapshot = validate_snapshot(snapshot_files(_docs()))
    port = ReliableDeliveryRuntime(DEFINITION, snapshot, pool=object())
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
        device_type="wind_turbine",
        device_id=DEVICE,
        point_table_id="point_table_wtg_sim_v1.0.0",
        device_config_id="device_farm-a_v1.0.0",
        sample_time=now,
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


def _encoded(batch: SampleBatch) -> tuple[bytes, ...]:
    return (
        encode_kafka_wide(
            batch,
            batch.samples,
            device_config_id=batch.device_config_id,
            physical_points=tuple(item.point_id for item in batch.samples),
            data_level="protocol",
        ),
    )


def test_kafka_target_emits_one_complete_device_row() -> None:
    adapter = _RecordingAdapter()
    port = _port(kafka_target=True, adapter=adapter)
    batch = _batch(500)
    message = _FakeJetStreamMessage()

    assert asyncio.run(port._deliver_batch(message, batch)) is True
    assert len(adapter.encoded_calls) == 1
    payloads, key, stripe = adapter.encoded_calls[0]
    assert adapter.legacy_calls == []
    assert message.in_progress_calls == 0
    assert key == f"{FARM}:{DEVICE}".encode()
    assert stripe == crc32(key) % 128
    assert payloads == _encoded(batch)
    row = WideRecord.from_kafka_json(
        payloads[0], physical_points=tuple(item.point_id for item in batch.samples),
    )
    assert len(row.values) == 500
    assert port.received_total == 500
    assert port.delivered_total == 1
    assert port.write_success_total == 1


def test_1024_points_are_not_split_into_partial_kafka_rows() -> None:
    adapter = _RecordingAdapter()
    port = _port(kafka_target=True, adapter=adapter)
    message = _FakeJetStreamMessage()
    batch = _batch(1024)

    assert asyncio.run(port._deliver_batch(message, batch)) is True
    assert len(adapter.encoded_calls) == 1
    assert len(adapter.encoded_calls[0][0]) == 1
    assert message.in_progress_calls == 0
    assert len(WideRecord.from_kafka_json(
        adapter.encoded_calls[0][0][0],
        physical_points=tuple(item.point_id for item in batch.samples),
    ).values) == 1024
    assert port.received_total == 1024
    assert port.delivered_total == 1


def test_non_kafka_target_uses_point_adapter_without_wide_encoding() -> None:
    adapter = _LegacyOnlyAdapter()
    port = _port(kafka_target=False, adapter=adapter)

    assert asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(500))) is True
    assert adapter.legacy_calls == 1
    assert port.received_total == 500
    assert port.delivered_total == 500


def test_kafka_target_refuses_point_subset_without_partial_write() -> None:
    snapshot = validate_snapshot(snapshot_files(_docs(points=["p001", "p002"])))
    adapter = _RecordingAdapter()
    port = ReliableDeliveryRuntime(DEFINITION, snapshot, pool=object())
    port._adapter = adapter
    port._kafka_target = True

    with pytest.raises(ValueError, match="complete device samples"):
        asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(10)))
    assert adapter.encoded_calls == []
    assert adapter.legacy_calls == []
    assert port.delivered_total == 0


def test_kafka_failure_does_not_report_device_cycle_success() -> None:
    adapter = _RecordingAdapter(outcome=BatchOutcome((), (), "not queued"))
    port = _port(kafka_target=True, adapter=adapter)
    assert asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(5))) is False
    assert port.delivered_total == 0
    assert port.write_success_total == 0


def test_kafka_rejects_permanent_point_classification() -> None:
    adapter = _RecordingAdapter(
        outcome=BatchOutcome(
            delivered=(), settled=(0,), error="", successful_attempts=0,
            permanent=(0,),
        ),
    )
    port = _port(kafka_target=True, adapter=adapter)
    with pytest.raises(RuntimeError, match="permanent point failures"):
        asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(2)))


def test_unknown_kafka_write_remains_unconfirmed_after_settle() -> None:
    class _UncertainAdapter(_RecordingAdapter):
        async def write_encoded_batch(
            self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int,
        ) -> BatchOutcome:
            self.encoded_calls.append((payloads, key, stripe))
            return BatchOutcome((), (), "Sink I/O result uncertain; waiting for settlement")

    adapter = _UncertainAdapter()
    port = _port(kafka_target=True, adapter=adapter)
    assert asyncio.run(port._deliver_batch(_FakeJetStreamMessage(), _batch(3))) is False
    assert adapter.settle_calls == 1
    assert port.delivered_total == 0
