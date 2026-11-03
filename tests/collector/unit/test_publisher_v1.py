"""Collector NATS sample transport and bounded queue unit tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from collector.infrastructure.publisher import CollectorPublisher
from contracts.sample_message import SampleMessage
from core.application import ProtocolSample

pytestmark = pytest.mark.unit


def _sample(point_id: str = "power") -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=1200,
        timestamp=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_realtime_message_contains_expiry_and_source_identity() -> None:
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm",
        collector_id="collector",
        delivery="realtime",
        capacity=2,
        max_staleness_ms=500,
    )
    await publisher.publish("wt01", (_sample(),))
    message: SampleMessage = publisher._queue.get_nowait()
    assert message.wind_farm_id == "farm"
    assert message.device_id == "wt01"
    assert message.task_id == "wt01"
    assert message.point_id == "power"
    assert message.max_staleness_ms == 500
    assert message.subject("realtime") == "wh.rt.farm.collector.wt01.power"
    assert SampleMessage.decode(message.encode(), "realtime") == message


@pytest.mark.asyncio
async def test_reliable_message_does_not_set_realtime_expiry() -> None:
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm",
        collector_id="collector",
        delivery="reliable",
        capacity=2,
    )
    await publisher.publish("wt01", (_sample(),))
    message = publisher._queue.get_nowait()
    assert message.max_staleness_ms is None
    assert message.subject("reliable") == "wh.hist.farm.collector.wt01.power"
    assert SampleMessage.decode(message.encode(), "reliable") == message


@pytest.mark.asyncio
async def test_publisher_capacity_is_bounded() -> None:
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm",
        collector_id="collector",
        delivery="realtime",
        capacity=1,
    )
    await publisher.publish("wt01", (_sample("a"), _sample("b")))
    assert publisher._queue.qsize() == 1
    assert publisher.backpressure == 1


@pytest.mark.asyncio
async def test_site_change_affects_only_new_messages() -> None:
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="old",
        collector_id="collector",
        delivery="realtime",
        capacity=2,
    )
    await publisher.publish("wt01", (_sample(),))
    publisher.update_site("new")
    await publisher.publish("wt01", (_sample(),))
    assert publisher._queue.get_nowait().wind_farm_id == "old"
    assert publisher._queue.get_nowait().wind_farm_id == "new"


@pytest.mark.asyncio
async def test_connect_failure_is_one_dropped_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failure must not add failed to the dropped final outcome."""

    async def disconnect(**_kwargs: object) -> None:
        raise ConnectionError("NATS is unreachable")

    monkeypatch.setattr("collector.infrastructure.publisher.nats.connect", disconnect)
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm",
        collector_id="collector",
        delivery="reliable",
    )
    await publisher.publish("wt01", (_sample(),))
    await publisher.start()
    try:
        for _ in range(100):
            if publisher.connection_failed:
                break
            await asyncio.sleep(0.01)
        assert publisher.connection_failed == 1
        assert publisher.dropped == 1
        assert publisher.not_attempted == 1
        assert publisher.failed == 0
        assert publisher.unconfirmed == 0
    finally:
        await publisher.stop()
