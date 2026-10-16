"""ListenerRegistry：过滤、溢出丢最旧、异常隔离、在途回调收敛、关闭语义。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core.application.protocol_contract import ProtocolSample
from core.infrastructure.protocol.iec104.listener import ListenerRegistry


def _sample(point_id: str, value: float = 1.0) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=value,
        timestamp=datetime.now(UTC),
    )


async def test_filter_and_fan_out_to_multiple_listeners() -> None:
    registry = ListenerRegistry()
    a: list[str] = []
    b: list[str] = []

    async def collect_a(sample: ProtocolSample) -> None:
        a.append(sample.point_id)

    async def collect_b(sample: ProtocolSample) -> None:
        b.append(sample.point_id)

    id_a = registry.add(("p1",), collect_a)
    registry.add((), collect_b)
    registry.offer(_sample("p1"))
    registry.offer(_sample("p2"))
    await asyncio.sleep(0.02)
    assert a == ["p1"]
    assert b == ["p1", "p2"]
    await registry.remove(id_a)
    await registry.close()


async def test_offer_without_listeners_does_not_queue() -> None:
    registry = ListenerRegistry(capacity=2)
    registry.offer(_sample("p1"))
    registry.offer(_sample("p2"))
    assert registry.dropped_count == 0
    await registry.close()


async def test_overflow_drops_oldest_and_counts() -> None:
    registry = ListenerRegistry(capacity=2)
    gate = asyncio.Event()
    received: list[float] = []

    async def gated(sample: ProtocolSample) -> None:
        await gate.wait()
        received.append(float(sample.value))  # type: ignore[arg-type]

    registry.add((), gated)
    # 第一条被 worker 取走并阻塞在 gate；队列容量 2，再投 4 条。
    registry.offer(_sample("p", 0.0))
    await asyncio.sleep(0.02)
    for i in range(1, 5):
        registry.offer(_sample("p", float(i)))
    assert registry.dropped_count == 2
    gate.set()
    await asyncio.sleep(0.05)
    # worker 持有 0；队列里最终是最新两条 3,4；1,2 被丢。
    assert received == [0.0, 3.0, 4.0]
    await registry.close()


async def test_offer_never_blocks_when_queue_full() -> None:
    registry = ListenerRegistry(capacity=1)
    gate = asyncio.Event()

    async def gated(_sample: ProtocolSample) -> None:
        await gate.wait()

    registry.add((), gated)
    registry.offer(_sample("p", 0.0))
    await asyncio.sleep(0.02)
    # 队列满后大量 offer 必须立即返回（不阻塞接收任务）。
    for i in range(100):
        registry.offer(_sample("p", float(i)))
    gate.set()
    await asyncio.sleep(0.02)
    assert registry.dropped_count == 99
    await registry.close()


async def test_throwing_callback_is_isolated() -> None:
    registry = ListenerRegistry()
    received: list[str] = []

    async def boom(_sample: ProtocolSample) -> None:
        raise RuntimeError("boom")

    async def collect(sample: ProtocolSample) -> None:
        received.append(sample.point_id)

    registry.add(("p1",), boom)
    registry.add((), collect)
    registry.offer(_sample("p1"))
    registry.offer(_sample("p2"))
    await asyncio.sleep(0.05)
    assert received == ["p1", "p2"]
    await registry.close()


async def test_remove_waits_for_in_flight_callback() -> None:
    registry = ListenerRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow(_sample: ProtocolSample) -> None:
        entered.set()
        await release.wait()
        finished.set()

    listener_id = registry.add((), slow)
    registry.offer(_sample("p1"))
    await entered.wait()

    remove_task = asyncio.create_task(registry.remove(listener_id))
    await asyncio.sleep(0.05)
    assert not remove_task.done()  # 在途回调未结束前不返回
    assert not finished.is_set()
    release.set()
    await asyncio.wait_for(remove_task, timeout=1)
    assert finished.is_set()

    # 取消后不再投递；remove 幂等。
    registry.offer(_sample("p2"))
    await registry.remove(listener_id)
    await asyncio.sleep(0.02)
    assert registry.active is False
    await registry.close()


async def test_close_is_idempotent_and_drains_pending() -> None:
    registry = ListenerRegistry(capacity=10)
    received: list[str] = []
    gate = asyncio.Event()

    async def collect(sample: ProtocolSample) -> None:
        received.append(sample.point_id)
        if sample.point_id == "p1":
            await gate.wait()

    registry.add((), collect)
    registry.offer(_sample("p1"))
    # 等 p1 实际投递（回调挂在 gate 上），再让 p2 排队。
    while not received:
        await asyncio.sleep(0.005)
    registry.offer(_sample("p2"))
    close_task = asyncio.create_task(registry.close())
    await asyncio.sleep(0.02)
    gate.set()
    await close_task
    await registry.close()
    assert received == ["p1"]  # p2 在队列中被清空，不投递
    with pytest.raises(RuntimeError, match="closed"):
        registry.add((), collect)
