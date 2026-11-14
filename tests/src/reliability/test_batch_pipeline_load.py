"""30 风机 × 10Hz 批次管线有界负载与资源释放可靠性测试。

场景对应验收 S4：Realtime 每批 100 点、Reliable 每批 500 点，在短测量窗口内
经真实 CollectorPublisher（Fake NATS 仅隔离 socket）持续入队/发送，验证：
- 计数闭合（attempted/published/dropped/failed 可对账）；
- Reliable 零静默丢失，Realtime 背压可解释；
- stop 后无 asyncio 任务残留、连接被关闭。

崩溃恢复独立审查：Reliable 队列是进程内 asyncio.Queue，无 WAL/持久化，
进程崩溃时未 PubAck 的批次无法保证恢复（见验收报告独立审查项），本测试
不声称端到端无损。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from collector.infrastructure.adapters.outbound import publisher as publisher_module
from collector.infrastructure.adapters.outbound.publisher import CollectorPublisher
from core.application import ProtocolSample

pytestmark = [pytest.mark.reliability, pytest.mark.soak, pytest.mark.load]


class _FakeJetStream:
    async def publish(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeNats:
    def __init__(self) -> None:
        self.realtime_calls = 0
        self.js_calls = 0
        self.is_connected = True
        self.closed = False

    async def publish(self, subject: str, payload: bytes) -> None:
        self.realtime_calls += 1

    def jetstream(self) -> _FakeJetStream:
        outer = self

        class _JS(_FakeJetStream):
            async def publish(self, *args: Any, **kwargs: Any) -> None:
                outer.js_calls += 1

        return _JS()

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _samples(points: int) -> tuple[ProtocolSample, ...]:
    now = datetime.now(UTC)
    return tuple(
        ProtocolSample(point_id=f"point.{i:04d}", value=1.0 + i, timestamp=now)
        for i in range(points)
    )


async def _drive(publisher: CollectorPublisher, devices: int, cycles: int, points: int) -> None:
    samples = _samples(points)
    for _cycle in range(cycles):
        await asyncio.gather(
            *(publisher.publish(f"wtg-{device:02d}", samples) for device in range(devices))
        )


@pytest.mark.asyncio
async def test_realtime_30_devices_10hz_load_counts_close_and_tasks_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeNats()

    async def fake_connect(**kwargs: Any) -> _FakeNats:
        return fake

    monkeypatch.setattr(publisher_module.nats, "connect", fake_connect)
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm-a",
        collector_id="collector-1",
        delivery="realtime",
        capacity=8192,
        max_samples_per_batch=100,
        # 放大 TTL 隔离容量/对账语义；默认 500ms TTL 的过期丢弃由
        # tests/src/component/test_collector/test_publisher_v2.py 专门验证。
        max_staleness_ms=30_000,
    )
    tasks_before = len(asyncio.all_tasks())
    expected_batches = 30 * 20
    await publisher.start()
    await _drive(publisher, devices=30, cycles=20, points=100)
    # 停机前等待稳态发布完成，避免 stop 的 2s drain 截止与容量结论混淆。
    deadline = asyncio.get_event_loop().time() + 15
    while publisher.published < expected_batches and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    await publisher.stop()

    assert fake.realtime_calls == expected_batches
    assert publisher.published == expected_batches
    assert publisher.published_samples == expected_batches * 100
    assert publisher.dropped == 0 and publisher.failed == 0
    assert publisher.not_attempted == 0
    assert fake.closed
    # stop 取消并 join 发布循环：不得残留该 publisher 的任务。
    assert len(asyncio.all_tasks()) <= tasks_before


@pytest.mark.asyncio
async def test_reliable_30_devices_load_has_no_silent_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeNats()

    async def fake_connect(**kwargs: Any) -> _FakeNats:
        return fake

    monkeypatch.setattr(publisher_module.nats, "connect", fake_connect)
    publisher = CollectorPublisher(
        servers=["nats://127.0.0.1:4222"],
        site_id="farm-a",
        collector_id="collector-1",
        delivery="reliable",
        capacity=8192,
        max_samples_per_batch=500,
    )
    expected_batches = 30 * 5
    await publisher.start()
    await _drive(publisher, devices=30, cycles=5, points=500)
    deadline = asyncio.get_event_loop().time() + 15
    while publisher.pubacked < expected_batches and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    await publisher.stop()

    assert fake.js_calls == expected_batches
    assert publisher.pubacked == expected_batches
    assert publisher.pubacked_samples == expected_batches * 500
    # Reliable 路径不允许丢弃；任何失败都必须显式计数。
    assert publisher.dropped == 0
    assert publisher.failed == 0 and publisher.not_attempted == 0
    assert fake.closed
