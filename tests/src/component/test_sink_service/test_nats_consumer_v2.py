"""NatsSinkConsumer v2 的 ACK/NAK/DLQ 责任边界组件测试。

Fake NATS Msg/Client 隔离传输；解码、Handler 失败、DLQ 发布与确认策略均
走真实生产代码。真实 JetStream 重投语义见 integration 层。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from contracts.sample_batch import SampleBatch, SampleItem
from sink_service.infrastructure.nats_consumer import NatsSinkConsumer

pytestmark = [pytest.mark.component, pytest.mark.mock_service, pytest.mark.reliability]


class FakeSequence:
    def __init__(self, stream: int) -> None:
        self.stream = stream


@dataclass
class FakeMetadata:
    sequence: FakeSequence
    num_delivered: int = 1


@dataclass
class FakeMsg:
    data: bytes
    subject: str
    reliable: bool
    acks: list[str] = field(default_factory=list)
    nak_delays: list[float] = field(default_factory=list)
    num_delivered: int = 1

    @property
    def metadata(self) -> FakeMetadata:
        return FakeMetadata(FakeSequence(42), num_delivered=self.num_delivered)

    async def ack(self) -> None:
        self.acks.append("ack")

    async def nak(self, delay: float | None = None) -> None:
        self.nak_delays.append(delay or 0.0)


class FakeJetStream:
    def __init__(self) -> None:
        self.dlq: list[dict[str, Any]] = []

    async def publish(
        self, subject: str, payload: bytes, timeout: float, headers: dict[str, str]
    ) -> None:
        self.dlq.append(
            {"subject": subject, "payload": payload, "timeout": timeout, "headers": headers}
        )


class FakeClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.js = FakeJetStream()
        self.closed = False

    def jetstream(self) -> FakeJetStream:
        return self.js

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _batch(*, mode: str = "reliable", point: str = "p1", value: object = 1) -> SampleBatch:
    return SampleBatch(
        batch_id="b-1",
        mode=mode,  # type: ignore[arg-type]
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="wtg-01",
        device_id="wtg-01",
        publish_time=datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
        samples=(
            SampleItem(
                point_id=point,
                sample_id="s-1",
                value=value,
                quality="good",
                sample_time=datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
                max_staleness_ms=None if mode == "reliable" else 500,
            ),
        ),
    )


def _consumer(
    *, durable: str | None = "sink-1", handler: Any = None, on_error: Any = None
) -> NatsSinkConsumer:
    async def default_handler(_batch: SampleBatch) -> None:
        return None

    return NatsSinkConsumer(
        ["nats://127.0.0.1:4222"],
        "wh.hist.batch.farm-a.collector-1.wtg-01"
        if durable
        else "wh.rt.batch.farm-a.collector-1.wtg-01",
        durable=durable,
        handler=handler or default_handler,
        on_delivery_error=on_error,
    )


def test_constructor_validates_subject_durable_and_limits() -> None:
    async def handler(_batch: SampleBatch) -> None:
        return None

    with pytest.raises(ValueError, match="servers"):
        NatsSinkConsumer([], "wh.rt.batch.x", durable=None, handler=handler)
    with pytest.raises(ValueError, match="batch subscription subject"):
        NatsSinkConsumer(["nats://x"], "legacy.point.subject", durable=None, handler=handler)
    with pytest.raises(ValueError, match="subject token"):
        NatsSinkConsumer(["nats://x"], "wh.hist.batch.x.y.z", durable="bad name", handler=handler)
    with pytest.raises(ValueError, match="consumer limits"):
        NatsSinkConsumer(
            ["nats://x"],
            "wh.hist.batch.x.y.z",
            durable="sink",
            handler=handler,
            ack_wait_seconds=0,
        )


@pytest.mark.asyncio
async def test_reliable_decode_failure_goes_to_dlq_then_ack() -> None:
    client = FakeClient()
    errors: list[tuple[str, int]] = []
    consumer = _consumer(on_error=lambda reason, attempts: errors.append((reason, attempts)))
    consumer._client = client  # type: ignore[attr-defined]
    msg = FakeMsg(data=b"{not json", subject=consumer._subject, reliable=True)  # type: ignore[attr-defined]

    await consumer._on_reliable(msg)  # type: ignore[attr-defined]

    assert msg.acks == ["ack"]
    assert msg.nak_delays == []
    assert len(client.js.dlq) == 1
    record = client.js.dlq[0]
    assert record["subject"] == "wh.dlq.batch.sink-1"
    assert record["payload"] == b"{not json"
    assert record["headers"]["Wh-Source-Subject"] == msg.subject
    assert record["headers"]["Wh-Source-Sequence"] == "42"
    assert record["headers"]["Wh-Failure-Type"] == "ValueError"
    assert errors == []


@pytest.mark.asyncio
async def test_dlq_unavailable_preserves_ack_responsibility_with_nak() -> None:
    class BrokenJs(FakeJetStream):
        async def publish(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("jetstream down")

    client = FakeClient()
    client.js = BrokenJs()
    errors: list[tuple[str, int]] = []
    consumer = _consumer(on_error=lambda reason, attempts: errors.append((reason, attempts)))
    consumer._client = client  # type: ignore[attr-defined]
    msg = FakeMsg(data=b"{}", subject=consumer._subject, reliable=True)  # type: ignore[attr-defined]

    await consumer._on_reliable(msg)  # type: ignore[attr-defined]

    assert msg.acks == []
    assert msg.nak_delays == [30]
    assert errors and errors[0][1] == 1


@pytest.mark.asyncio
async def test_handler_failure_naks_with_capped_backoff_and_reports_attempts() -> None:
    async def exploding(_batch: SampleBatch) -> None:
        raise RuntimeError("sink write failed")

    errors: list[tuple[str, int]] = []
    consumer = _consumer(handler=exploding, on_error=lambda r, a: errors.append((r, a)))
    consumer._client = FakeClient()  # type: ignore[attr-defined]
    msg = FakeMsg(
        data=_batch().encode(),
        subject=consumer._subject,  # type: ignore[attr-defined]
        reliable=True,
        num_delivered=3,
    )

    await consumer._on_reliable(msg)  # type: ignore[attr-defined]

    assert msg.acks == []
    assert msg.nak_delays == [4.0]  # 2 ** min(3-1, 6)
    assert errors and errors[0][1] == 3


@pytest.mark.asyncio
async def test_reliable_success_acks_once() -> None:
    received: list[SampleBatch] = []

    async def handler(batch: SampleBatch) -> None:
        received.append(batch)

    consumer = _consumer(handler=handler)
    consumer._client = FakeClient()  # type: ignore[attr-defined]
    msg = FakeMsg(
        data=_batch().encode(),
        subject=consumer._subject,  # type: ignore[attr-defined]
        reliable=True,
    )

    await consumer._on_reliable(msg)  # type: ignore[attr-defined]

    assert msg.acks == ["ack"]
    assert len(received) == 1
    assert received[0].batch_id == "b-1"


@pytest.mark.asyncio
async def test_realtime_handler_failure_does_not_ack_and_reports() -> None:
    errors: list[tuple[str, int]] = []

    async def exploding(_batch: SampleBatch) -> None:
        raise RuntimeError("realtime sink unavailable")

    consumer = _consumer(
        durable=None, handler=exploding, on_error=lambda r, a: errors.append((r, a))
    )
    msg = FakeMsg(
        data=_batch(mode="realtime").encode(),
        subject=consumer._subject,  # type: ignore[attr-defined]
        reliable=False,
    )

    await consumer._on_realtime(msg)  # type: ignore[attr-defined]

    assert msg.acks == [] and msg.nak_delays == []
    assert errors and "realtime sink unavailable" in errors[0][0]
    assert errors[0][1] == 1


@pytest.mark.asyncio
async def test_subject_envelope_mismatch_rejects_before_handler() -> None:
    called: list[SampleBatch] = []

    async def handler(batch: SampleBatch) -> None:
        called.append(batch)

    consumer = _consumer(handler=handler)
    consumer._client = FakeClient()  # type: ignore[attr-defined]
    msg = FakeMsg(
        data=_batch().encode(),
        subject="wh.hist.batch.farm-a.collector-other.wtg-01",
        reliable=True,
    )

    await consumer._on_reliable(msg)  # type: ignore[attr-defined]

    assert called == []
    assert msg.acks == ["ack"]  # 不可解码消息进 DLQ 后确认
    assert consumer._client.js.dlq  # type: ignore[attr-defined]


def test_clear_guards_realtime_subscriber_and_active_client() -> None:
    realtime = _consumer(durable=None)
    with pytest.raises(RuntimeError, match="backlog"):
        asyncio.run(realtime.clear())
    durable = _consumer()
    durable._client = FakeClient()  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="stop Sink"):
        asyncio.run(durable.clear())
