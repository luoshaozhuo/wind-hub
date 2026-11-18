"""R4-G G-03：Reliable 目标确认 sample_evidence 的纯状态机契约。

不启动 JetStream：直接驱动 ``_deliver_batch``，钉死“哪些样本允许进入诊断
证据”的判定边界。ACK 顺序与真实重投由 real NATS 集成套件
（test_reliable_durable_ack_real_nats.py）承担，Mock 不冒充 Broker 证据。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.realtime import BatchOutcome
from sink_service.infrastructure.config_io import validate_snapshot
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryRuntime
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files

pytestmark = [pytest.mark.unit, pytest.mark.contract]

_DELIVERY = "power_history"
_FARM = "farm-a"
_COLLECTOR = "collector-1"
_DEVICE = "WT001"


class _FakeMessage:
    def __init__(self) -> None:
        self.extends = 0
        self.acks = 0

    async def extend(self) -> None:
        self.extends += 1

    async def ack(self, timeout: float = 5) -> None:
        self.acks += 1


class _FakeKafkaAdapter:
    def __init__(self, outcome: BatchOutcome) -> None:
        self._outcome = outcome
        self.io_inflight = False

    async def settle(self) -> BatchOutcome | None:
        return None

    async def write_encoded_batch(
        self, payloads: tuple[bytes, ...], *, key: bytes, stripe: int
    ) -> BatchOutcome:
        return self._outcome


def _delivery_document(points: list[str]) -> dict:
    return {
        "template_id": "reliable_history",
        "template_version": "1.0.0",
        "sink_id": "history_kafka",
        "desired_state": "START",
        "source": {
            "wind_farm_id": _FARM,
            "collector_id": _COLLECTOR,
            "devices": ["WT001", "WT002"],
            "points": points,
        },
        "overrides": {},
    }


def _port(adapter: _FakeKafkaAdapter, points: list[str] | None = None) -> ReliableDeliveryRuntime:
    selected = points or ["active_power"]
    docs = snapshot_docs(
        deliveries={_DELIVERY: _delivery_document(selected)},
        sinks={
            "history_kafka": {
                "template_id": "kafka_history",
                "template_version": "1.0.0",
                "connection": {"bootstrap_servers": ["127.0.0.1:19092"]},
                "overrides": {},
            }
        },
        include_templates=("kafka_history", "reliable_history"),
    )
    snapshot = validate_snapshot(snapshot_files(docs))
    definition = DeliveryDefinition(_DELIVERY, "history_kafka", "reliable", "START")
    port = ReliableDeliveryRuntime(definition, snapshot, pool=object())
    port._validate_target()
    port._adapter = adapter
    return port


def _batch(samples: list[tuple[str, str]], *, device: str = _DEVICE) -> SampleBatch:
    now = datetime.now(UTC)
    return SampleBatch(
        batch_id="b-evidence",
        mode="reliable",
        wind_farm_id=_FARM,
        collector_id=_COLLECTOR,
        task_id="task-qa",
        device_id=device,
        publish_time=now,
        samples=tuple(
            SampleItem(
                point_id=point_id,
                sample_id=sample_id,
                value=1.5,
                quality="good",
                sample_time=now,
                receive_time=now,
            )
            for point_id, sample_id in samples
        ),
    )


async def test_confirmed_samples_remain_pending_until_source_ack() -> None:
    adapter = _FakeKafkaAdapter(
        BatchOutcome(delivered=(0, 1, 2), settled=(0, 1, 2), successful_attempts=1)
    )
    port = _port(adapter, ["p1", "p2", "p3"])
    message = _FakeMessage()
    terminal = await port._deliver_batch(
        message, _batch([("p1", "s1"), ("p2", "s2"), ("p3", "s3")])
    )

    assert terminal is True
    assert len(port._pending_confirmed_samples) == 3
    # Promotion to the diagnostic surface happens only after source ACK in _run.
    assert len(port._confirmed_samples) == 0
    assert message.acks == 0
    assert port._pending_confirmed_samples[0] == {
        "device_id": _DEVICE,
        "sample_id": "s1",
        "delivery_id": _DELIVERY,
        "channel": "reliable",
    }


async def test_failed_target_write_produces_no_confirmed_samples() -> None:
    adapter = _FakeKafkaAdapter(
        BatchOutcome(delivered=(), settled=(0, 1), error="target outage", failed_attempts=1)
    )
    port = _port(adapter, ["p1", "p2"])
    terminal = await port._deliver_batch(_FakeMessage(), _batch([("p1", "s1"), ("p2", "s2")]))

    assert terminal is False
    assert port._pending_confirmed_samples == []
    assert len(port._confirmed_samples) == 0
    assert port.delivered_total == 0


async def test_partial_settlement_confirms_only_delivered_indices() -> None:
    # Index 1 is neither delivered nor permanently classified: it must not be
    # observable as target-confirmed evidence, and the batch stays unresolved.
    adapter = _FakeKafkaAdapter(
        BatchOutcome(
            delivered=(0,),
            settled=(0, 1),
            error="partial write uncertainty",
            successful_attempts=1,
            failed_attempts=1,
        )
    )
    port = _port(adapter, ["p1", "p2"])
    terminal = await port._deliver_batch(_FakeMessage(), _batch([("p1", "s1"), ("p2", "s2")]))

    assert terminal is False
    assert [item["sample_id"] for item in port._pending_confirmed_samples] == ["s1"]
    assert len(port._confirmed_samples) == 0


async def test_pending_evidence_is_capped_at_four_and_keeps_newest() -> None:
    adapter = _FakeKafkaAdapter(
        BatchOutcome(delivered=tuple(range(6)), settled=tuple(range(6)), successful_attempts=1)
    )
    port = _port(adapter, [f"p{i}" for i in range(6)])
    await port._deliver_batch(_FakeMessage(), _batch([(f"p{i}", f"s{i}") for i in range(6)]))

    assert len(port._pending_confirmed_samples) == 4
    assert [item["sample_id"] for item in port._pending_confirmed_samples] == [
        "s2",
        "s3",
        "s4",
        "s5",
    ]


async def test_pending_evidence_resets_for_each_source_batch() -> None:
    adapter = _FakeKafkaAdapter(BatchOutcome(delivered=(0,), settled=(0,), successful_attempts=1))
    port = _port(adapter)
    await port._deliver_batch(_FakeMessage(), _batch([("active_power", "old-1")]))
    assert [item["sample_id"] for item in port._pending_confirmed_samples] == ["old-1"]

    await port._deliver_batch(_FakeMessage(), _batch([("active_power", "new-1")]))
    assert [item["sample_id"] for item in port._pending_confirmed_samples] == ["new-1"]
    assert len(port._confirmed_samples) == 0


async def test_promoted_evidence_is_bounded_and_inspect_json_is_frame_safe() -> None:
    import json

    adapter = _FakeKafkaAdapter(
        BatchOutcome(delivered=tuple(range(6)), settled=tuple(range(6)), successful_attempts=1)
    )
    port = _port(adapter, [f"p{i}" for i in range(6)])
    await port._deliver_batch(_FakeMessage(), _batch([(f"p{i}", f"s{i}") for i in range(6)]))
    # Mirror the exact production promotion performed after message.ack().
    port._confirmed_samples.extend(port._pending_confirmed_samples[-4:])
    port._pending_confirmed_samples.clear()

    assert len(port._confirmed_samples) == 4
    encoded = port.inspect()["sample_evidence"]
    assert len(encoded.encode()) < 2048
    decoded = json.loads(encoded)
    assert len(decoded) == 4
    assert all(item["delivery_id"] == _DELIVERY for item in decoded)


async def test_evidence_is_isolated_between_delivery_engines() -> None:
    outcome = BatchOutcome(delivered=(0,), settled=(0,), successful_attempts=1)
    port_a = _port(_FakeKafkaAdapter(outcome))
    port_b = _port(_FakeKafkaAdapter(outcome))

    await port_a._deliver_batch(
        _FakeMessage(), _batch([("active_power", "a-only")], device="WT001")
    )
    await port_b._deliver_batch(
        _FakeMessage(), _batch([("active_power", "b-only")], device="WT002")
    )

    ids_a = {item["sample_id"] for item in port_a._pending_confirmed_samples}
    ids_b = {item["sample_id"] for item in port_b._pending_confirmed_samples}
    assert ids_a == {"a-only"} and ids_b == {"b-only"}
    assert port_b._pending_confirmed_samples[0]["device_id"] == "WT002"


def test_cold_inspect_exposes_empty_sample_evidence() -> None:
    port = _port(_FakeKafkaAdapter(BatchOutcome(delivered=(), settled=())))
    assert port.inspect()["sample_evidence"] == "[]"


@pytest.mark.parametrize(
    "outcome",
    [
        BatchOutcome(delivered=(), settled=(0,), error="disk full", failed_attempts=1),
        BatchOutcome(
            delivered=(),
            settled=(0,),
            error="Sink I/O result uncertain; waiting for settlement",
        ),
    ],
)
async def test_uncertain_or_failed_writes_never_become_success_evidence(
    outcome: BatchOutcome,
) -> None:
    port = _port(_FakeKafkaAdapter(outcome))
    terminal = await port._deliver_batch(_FakeMessage(), _batch([("active_power", "s1")]))
    assert terminal is False
    assert port._pending_confirmed_samples == []
    assert port.inspect()["sample_evidence"] == "[]"
    assert port.last_error == ""  # engine-level error is recorded only on NAK in _run
