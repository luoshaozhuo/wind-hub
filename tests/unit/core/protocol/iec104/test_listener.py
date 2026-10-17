"""ListenerRegistry：过滤、溢出丢最旧、异常隔离、有界超时、取消与关闭语义。"""

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


async def test_one_normal_and_one_throwing_listener() -> None:
    """异常监听者不影响正常监听者，也不杀死 worker。"""
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


async def test_slow_listener_does_not_block_others_beyond_timeout() -> None:
    """一个监听者长时间阻塞：超时取消后其他监听者继续收到后续样本。"""
    registry = ListenerRegistry(callback_timeout=0.05)
    block = asyncio.Event()
    blocked_started = asyncio.Event()
    healthy: list[str] = []
    healthy_done = asyncio.Event()

    async def slow(_sample: ProtocolSample) -> None:
        blocked_started.set()
        await block.wait()

    async def collect(sample: ProtocolSample) -> None:
        healthy.append(sample.point_id)
        if len(healthy) == 2:
            healthy_done.set()

    registry.add((), slow)
    registry.add(("p1", "p2"), collect)
    registry.offer(_sample("p1"))
    await blocked_started.wait()
    # 第一条样本的分发先遇到慢监听者；其超时取消后 p1 才到 collect。
    registry.offer(_sample("p2"))
    await asyncio.wait_for(healthy_done.wait(), timeout=1.0)
    assert healthy == ["p1", "p2"]
    block.set()
    await registry.close()


async def test_callback_timeout_is_cancelled_and_listener_recovers() -> None:
    """超时后回调被取消；回调可协作结束，监听者随后继续收到样本。"""
    registry = ListenerRegistry(callback_timeout=0.05)
    received: list[str] = []
    second = asyncio.Event()

    async def sometimes_slow(sample: ProtocolSample) -> None:
        if sample.point_id == "slow":
            await asyncio.sleep(5)
        received.append(sample.point_id)
        if sample.point_id == "fast2":
            second.set()

    listener_id = registry.add(("slow", "fast1", "fast2"), sometimes_slow)
    registry.offer(_sample("slow"))
    await asyncio.sleep(0.2)  # 等超时取消完成
    registry.offer(_sample("fast1"))
    registry.offer(_sample("fast2"))
    await asyncio.wait_for(second.wait(), timeout=1.0)
    assert received == ["fast1", "fast2"]
    # 取消后的回调任务没有残留。
    await registry.remove(listener_id)
    await registry.close()


async def test_uncooperative_callback_makes_remove_raise_timeout() -> None:
    """吞掉 CancelledError 的回调无法在时限内结束：remove 显式失败。"""
    registry = ListenerRegistry(callback_timeout=0.05)
    entered = asyncio.Event()
    allow_finish = asyncio.Event()

    async def stubborn(_sample: ProtocolSample) -> None:
        entered.set()
        for _ in range(5):
            try:
                await allow_finish.wait()
                return
            except asyncio.CancelledError:
                # 故意吞掉取消继续阻塞：不可协作回调。
                continue

    listener_id = registry.add((), stubborn)
    registry.offer(_sample("p1"))
    await entered.wait()
    await asyncio.sleep(0.15)  # 等 worker 超时并进入取消后清理窗口
    with pytest.raises(TimeoutError):
        await registry.remove(listener_id, timeout=0.05)
    # 放行后任务实际结束；注册表状态不被晚到的结束破坏。
    allow_finish.set()
    await asyncio.sleep(0.1)
    await registry.close()


async def test_callback_runs_cleanup_during_cancellation() -> None:
    """remove 等待回调的取消清理代码执行完毕后才成功返回。"""
    registry = ListenerRegistry()
    cleanup_done = asyncio.Event()
    entered = asyncio.Event()

    async def with_cleanup(_sample: ProtocolSample) -> None:
        entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)  # 模拟取消清理
            cleanup_done.set()
            raise

    listener_id = registry.add((), with_cleanup)
    registry.offer(_sample("p1"))
    await entered.wait()
    remove_task = asyncio.create_task(registry.remove(listener_id))
    await asyncio.wait_for(remove_task, timeout=1.0)
    assert cleanup_done.is_set()
    await registry.close()


async def test_remove_before_delivery_is_immediate_and_queued_sample_skipped() -> None:
    """未在执行中的监听立即取消；已入队样本不再投递给它。"""
    registry = ListenerRegistry(capacity=10)
    received: list[str] = []

    async def collect(sample: ProtocolSample) -> None:
        received.append(sample.point_id)

    listener_id = registry.add(("p1", "p2"), collect)
    registry.offer(_sample("p3"))  # 不匹配过滤
    registry.offer(_sample("p1"))
    await registry.remove(listener_id)
    await asyncio.sleep(0.05)
    assert received == []
    assert registry.active is False
    await registry.close()


async def test_remove_is_idempotent() -> None:
    registry = ListenerRegistry()
    listener_id = registry.add(("p",), lambda _s: _noop())
    await registry.remove(listener_id)
    await registry.remove(listener_id)  # 重复 unlisten 不报错
    await registry.close()
    await registry.close()


async def _noop() -> None:
    pass


async def test_unlisten_concurrent_with_delivery() -> None:
    """unlisten 与持续投递并发：取消后绝不再有该监听者的新回调。"""
    registry = ListenerRegistry(callback_timeout=0.1)
    active = 0
    max_concurrent = 0

    async def collector(sample: ProtocolSample) -> None:
        nonlocal active, max_concurrent
        active += 1
        max_concurrent = max(max_concurrent, active)
        try:
            await asyncio.sleep(0.005)
        finally:
            active -= 1

    listener_id = registry.add(("p",), collector)

    async def offer_loop() -> None:
        for i in range(100):
            registry.offer(_sample("p", float(i)))
            await asyncio.sleep(0)

    offer_task = asyncio.create_task(offer_loop())
    await asyncio.sleep(0.02)
    await registry.remove(listener_id)
    await offer_task
    assert active == 0
    assert max_concurrent == 1  # 单监听者回调从不并发
    await registry.close()


async def test_unlisten_concurrent_with_close() -> None:
    registry = ListenerRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(_sample: ProtocolSample) -> None:
        entered.set()
        await release.wait()

    listener_id = registry.add((), slow)
    registry.offer(_sample("p1"))
    await entered.wait()
    remove_task = asyncio.create_task(registry.remove(listener_id))
    close_task = asyncio.create_task(registry.close())
    await asyncio.sleep(0.02)
    release.set()
    # 两者都在等待同一个回调收敛：要么 remove 成功，要么 close 收敛，
    # 但不得抛出、不得遗留任务。
    await asyncio.wait_for(asyncio.gather(remove_task, close_task), timeout=1.0)
    await registry.close()


async def test_offer_after_close_is_dropped_and_add_rejected() -> None:
    registry = ListenerRegistry()
    received: list[str] = []
    registry.add((), lambda s: _append(received, s))
    await registry.close()
    registry.offer(_sample("p1"))  # 关闭后投递直接丢弃
    await asyncio.sleep(0.02)
    assert received == []
    with pytest.raises(RuntimeError, match="closed"):
        registry.add((), lambda s: _append(received, s))


async def _append(target: list[str], sample: ProtocolSample) -> None:
    target.append(sample.point_id)


async def test_close_leaves_no_residual_worker_or_callback_tasks() -> None:
    registry = ListenerRegistry(callback_timeout=0.05)
    slow_started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_sample: ProtocolSample) -> None:
        slow_started.set()
        await release.wait()

    registry.add((), slow)
    registry.offer(_sample("p1"))
    await slow_started.wait()
    close_task = asyncio.create_task(registry.close())
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.wait_for(close_task, timeout=1.0)

    tasks = [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and "iec104-listen" in (task.get_name() or "")
    ]
    assert tasks == []


async def test_close_cancels_in_flight_callback_and_waits_for_cleanup() -> None:
    registry = ListenerRegistry()
    cleanup_done = asyncio.Event()
    entered = asyncio.Event()

    async def with_cleanup(_sample: ProtocolSample) -> None:
        entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cleanup_done.set()
            raise

    registry.add((), with_cleanup)
    registry.offer(_sample("p1"))
    await entered.wait()
    await asyncio.wait_for(registry.close(), timeout=1.0)
    assert cleanup_done.is_set()
