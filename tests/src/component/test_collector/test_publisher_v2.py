"""CollectorPublisher v2（SampleBatch 批次管线）组件测试。

Fake NATS 只隔离传输 I/O，批次封装、背压、过期、重投身份与计数语义均为
真实生产代码路径。真实 NATS/JetStream 链路见 integration 层测试。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from collector.infrastructure.adapters.outbound import publisher as publisher_module
from collector.infrastructure.adapters.outbound.publisher import CollectorPublisher
from core.application import ProtocolSample

pytestmark = [pytest.mark.component, pytest.mark.mock_service, pytest.mark.overload]


class FakeJetStream:
    def __init__(self, client: FakeNatsClient) -> None:
        self._client = client

    async def publish(
        self, subject: str, payload: bytes, timeout: float, headers: dict[str, str]
    ) -> None:
        self._client.jetstream_calls.append(
            {"subject": subject, "payload": payload, "timeout": timeout, "headers": headers}
        )
        if self._client.fail_next:
            self._client.fail_next -= 1
            raise ConnectionError("simulated JetStream outage")


class FakeNatsClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.published: list[dict[str, Any]] = []
        self.jetstream_calls: list[dict[str, Any]] = []
        self.fail_next = 0
        self.closed = False
        self.drained = False

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append({"subject": subject, "payload": payload})

    def jetstream(self) -> FakeJetStream:
        return FakeJetStream(self)

    async def drain(self) -> None:
        self.drained = True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_nats(monkeypatch: pytest.MonkeyPatch) -> FakeNatsClient:
    client = FakeNatsClient()

    async def fake_connect(**kwargs: Any) -> FakeNatsClient:
        client.connect_kwargs = kwargs  # type: ignore[attr-defined]
        return client

    monkeypatch.setattr(publisher_module.nats, "connect", fake_connect)
    return client


def _sample(point_id: str = "power", *, age_ms: int = 0, value: object = 1200) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=value,  # type: ignore[arg-type]
        timestamp=datetime.now(UTC) - timedelta(milliseconds=age_ms),
    )


def _publisher(delivery: str = "realtime", **kwargs: Any) -> CollectorPublisher:
    options: dict[str, Any] = {
        "servers": ["nats://127.0.0.1:4222"],
        "site_id": "farm-a",
        "collector_id": "collector-1",
        "delivery": delivery,
        "capacity": 8,
    }
    options.update(kwargs)
    return CollectorPublisher(**options)


def test_constructor_validates_delivery_servers_limits_and_staleness() -> None:
    with pytest.raises(ValueError, match="delivery"):
        _publisher("kafka")
    with pytest.raises(ValueError, match="servers"):
        CollectorPublisher(servers=[], site_id="f", collector_id="c", delivery="realtime")
    with pytest.raises(ValueError, match="batch limits"):
        _publisher(max_samples_per_batch=0)
    with pytest.raises(ValueError, match="staleness"):
        _publisher("realtime", max_staleness_ms=0)
    with pytest.raises(ValueError, match="subject token"):
        _publisher(site_id="bad site")


@pytest.mark.asyncio
async def test_realtime_source_batch_becomes_one_nats_publish(fake_nats: FakeNatsClient) -> None:
    publisher = _publisher()
    await publisher.start()
    await publisher.publish("wtg-01", (_sample("p1"), _sample("p2")))
    await publisher.stop()
    assert len(fake_nats.published) == 1
    record = fake_nats.published[0]
    assert record["subject"] == "wh.rt.batch.farm-a.collector-1.wtg-01"
    assert publisher.published == 1
    assert publisher.published_samples == 2
    assert publisher.client_submitted == 1
    assert fake_nats.jetstream_calls == []


@pytest.mark.asyncio
async def test_reliable_uses_jetstream_with_stable_nats_msg_id(
    fake_nats: FakeNatsClient,
) -> None:
    publisher = _publisher("reliable")
    await publisher.start()
    await publisher.publish("wtg-01", (_sample("p1"),))
    await publisher.stop()
    assert len(fake_nats.jetstream_calls) == 1
    call = fake_nats.jetstream_calls[0]
    assert call["subject"] == "wh.hist.batch.farm-a.collector-1.wtg-01"
    assert call["headers"]["Nats-Msg-Id"]
    assert publisher.pubacked == 1
    assert publisher.pubacked_samples == 1


@pytest.mark.asyncio
async def test_reliable_retry_keeps_original_batch_identity(fake_nats: FakeNatsClient) -> None:
    publisher = _publisher("reliable")
    fake_nats.fail_next = 2  # 前两次 PubAck 失败，第三次成功
    await publisher.start()
    await publisher.publish("wtg-01", (_sample("p1"),))
    deadline = asyncio.get_event_loop().time() + 10
    while not fake_nats.jetstream_calls and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    await publisher.stop()
    assert len(fake_nats.jetstream_calls) == 3
    msg_ids = {call["headers"]["Nats-Msg-Id"] for call in fake_nats.jetstream_calls}
    assert msg_ids == {fake_nats.jetstream_calls[0]["headers"]["Nats-Msg-Id"]}
    assert publisher.pubacked == 1
    assert publisher.unconfirmed >= 1


@pytest.mark.asyncio
async def test_realtime_backpressure_drops_without_raising(fake_nats: FakeNatsClient) -> None:
    publisher = _publisher(capacity=1)
    # 不启动消费循环：队列只能容纳 1 批，第二批走背压丢弃路径。
    await publisher.publish("wtg-01", (_sample("p1"),))
    await publisher.publish("wtg-01", (_sample("p2"),))
    assert publisher.backpressure == 1
    assert publisher.dropped == 1
    assert publisher.not_attempted == 1
    await publisher.start()
    await publisher.stop()
    assert len(fake_nats.published) == 1


@pytest.mark.asyncio
async def test_reliable_enqueue_timeout_is_failure_not_silent_drop(
    fake_nats: FakeNatsClient,
) -> None:
    publisher = _publisher("reliable", capacity=1, enqueue_timeout_s=0.05)
    await publisher.publish("wtg-01", (_sample("p1"),))
    with pytest.raises(RuntimeError, match="not accepted"):
        await publisher.publish("wtg-01", (_sample("p2"),))
    assert publisher.not_attempted >= 1
    assert publisher.failed >= 1


@pytest.mark.asyncio
async def test_realtime_expired_batch_is_dropped_and_counted(
    fake_nats: FakeNatsClient,
) -> None:
    publisher = _publisher(max_staleness_ms=100)
    await publisher.start()
    await publisher.publish("wtg-01", (_sample("p1", age_ms=1000),))
    await publisher.stop()
    assert publisher.expired == 1
    assert publisher.dropped == 1
    assert fake_nats.published == []


@pytest.mark.asyncio
async def test_duplicate_point_id_rejected_before_queueing(
    fake_nats: FakeNatsClient,
) -> None:
    publisher = _publisher()
    with pytest.raises(ValueError, match="rejected"):
        await publisher.publish("wtg-01", (_sample("p1"), _sample("p1")))
    assert publisher.failed == 1
    assert publisher.not_attempted == 1
    await publisher.start()
    await publisher.stop()
    assert fake_nats.published == []


@pytest.mark.asyncio
async def test_oversized_source_event_is_split_into_multiple_batches(
    fake_nats: FakeNatsClient,
) -> None:
    publisher = _publisher(max_samples_per_batch=2)
    await publisher.start()
    await publisher.publish("wtg-01", tuple(_sample(f"p{i}") for i in range(5)))
    await publisher.stop()
    assert len(fake_nats.published) == 3
    assert sum(len(call["payload"]) for call in fake_nats.published) > 0
    assert publisher.published_samples == 5


@pytest.mark.asyncio
async def test_stop_releases_connection(fake_nats: FakeNatsClient) -> None:
    publisher = _publisher()
    await publisher.start()
    await publisher.publish("wtg-01", (_sample("p1"),))
    await publisher.stop()
    assert fake_nats.drained and fake_nats.closed
