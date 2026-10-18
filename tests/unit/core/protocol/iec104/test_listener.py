"""ListenerRegistry：过滤、溢出丢最旧、异常隔离、有界超时、取消与关闭语义。"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest

from core.application.protocol_contract import ProtocolSample
from core.infrastructure.protocol.iec104.listener import (
    ListenerRegistry,
    ProtocolListenerCallback,
)


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

    async def collect_a(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
        a.append(sample.point_id)

    async def collect_b(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
        b.append(sample.point_id)

    id_a = registry.add(("p1",), collect_a)
    registry.add((), collect_b)
    registry.offer((_sample("p1"),))
    registry.offer((_sample("p2"),))
    await asyncio.sleep(0.02)
    assert a == ["p1"]
    assert b == ["p1", "p2"]
    await registry.remove(id_a)
    await registry.close()


async def test_offer_without_listeners_does_not_queue() -> None:
    registry = ListenerRegistry(capacity=2)
    registry.offer((_sample("p1"),))
    registry.offer((_sample("p2"),))
    assert registry.dropped_count == 0
    await registry.close()


async def test_overflow_drops_oldest_and_counts() -> None:
    registry = ListenerRegistry(capacity=2)
    gate = asyncio.Event()
    received: list[float] = []

    async def gated(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
        await gate.wait()
        received.append(float(sample.value))  # type: ignore[arg-type]

    registry.add((), gated)
    # 第一条被 worker 取走并阻塞在 gate；队列容量 2，再投 4 条。
    registry.offer((_sample("p", 0.0),))
    await asyncio.sleep(0.02)
    for i in range(1, 5):
        registry.offer((_sample("p", float(i)),))
    assert registry.dropped_count == 2
    gate.set()
    await asyncio.sleep(0.05)
    # worker 持有 0；队列里最终是最新两条 3,4；1,2 被丢。
    assert received == [0.0, 3.0, 4.0]
    await registry.close()


async def test_offer_never_blocks_when_queue_full() -> None:
    registry = ListenerRegistry(capacity=1)
    gate = asyncio.Event()

    async def gated(_samples: tuple[ProtocolSample, ...]) -> None:
        await gate.wait()

    registry.add((), gated)
    registry.offer((_sample("p", 0.0),))
    await asyncio.sleep(0.02)
    # 队列满后大量 offer 必须立即返回（不阻塞接收任务）。
    for i in range(100):
        registry.offer((_sample("p", float(i)),))
    gate.set()
    await asyncio.sleep(0.02)
    assert registry.dropped_count == 99
    await registry.close()


async def test_one_normal_and_one_throwing_listener() -> None:
    """异常监听者不影响正常监听者，也不杀死 worker。"""
    registry = ListenerRegistry()
    received: list[str] = []

    async def boom(_samples: tuple[ProtocolSample, ...]) -> None:
        raise RuntimeError("boom")

    async def collect(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
        received.append(sample.point_id)

    registry.add(("p1",), boom)
    registry.add((), collect)
    registry.offer((_sample("p1"),))
    registry.offer((_sample("p2"),))
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

    async def slow(_samples: tuple[ProtocolSample, ...]) -> None:
        blocked_started.set()
        await block.wait()

    async def collect(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
        healthy.append(sample.point_id)
        if len(healthy) == 2:
            healthy_done.set()

    registry.add((), slow)
    registry.add(("p1", "p2"), collect)
    registry.offer((_sample("p1"),))
    await blocked_started.wait()
    # 第一条样本的分发先遇到慢监听者；其超时取消后 p1 才到 collect。
    registry.offer((_sample("p2"),))
    await asyncio.wait_for(healthy_done.wait(), timeout=1.0)
    assert healthy == ["p1", "p2"]
    block.set()
    await registry.close()


async def test_callback_timeout_is_cancelled_and_listener_recovers() -> None:
    """超时后回调被取消；回调可协作结束，监听者随后继续收到样本。"""
    registry = ListenerRegistry(callback_timeout=0.05)
    received: list[str] = []
    second = asyncio.Event()

    async def sometimes_slow(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
        if sample.point_id == "slow":
            await asyncio.sleep(5)
        received.append(sample.point_id)
        if sample.point_id == "fast2":
            second.set()

    listener_id = registry.add(("slow", "fast1", "fast2"), sometimes_slow)
    registry.offer((_sample("slow"),))
    await asyncio.sleep(0.2)  # 等超时取消完成
    registry.offer((_sample("fast1"),))
    registry.offer((_sample("fast2"),))
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

    async def stubborn(_samples: tuple[ProtocolSample, ...]) -> None:
        entered.set()
        for _ in range(5):
            try:
                await allow_finish.wait()
                return
            except asyncio.CancelledError:
                # 故意吞掉取消继续阻塞：不可协作回调。
                continue

    listener_id = registry.add((), stubborn)
    registry.offer((_sample("p1"),))
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

    async def with_cleanup(_samples: tuple[ProtocolSample, ...]) -> None:
        entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)  # 模拟取消清理
            cleanup_done.set()
            raise

    listener_id = registry.add((), with_cleanup)
    registry.offer((_sample("p1"),))
    await entered.wait()
    remove_task = asyncio.create_task(registry.remove(listener_id))
    await asyncio.wait_for(remove_task, timeout=1.0)
    assert cleanup_done.is_set()
    await registry.close()


async def test_remove_before_delivery_is_immediate_and_queued_sample_skipped() -> None:
    """未在执行中的监听立即取消；已入队样本不再投递给它。"""
    registry = ListenerRegistry(capacity=10)
    received: list[str] = []

    async def collect(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
        received.append(sample.point_id)

    listener_id = registry.add(("p1", "p2"), collect)
    registry.offer((_sample("p3"),))  # 不匹配过滤
    registry.offer((_sample("p1"),))
    await registry.remove(listener_id)
    await asyncio.sleep(0.05)
    assert received == []
    assert registry.active is False
    await registry.close()


async def test_remove_is_idempotent() -> None:
    registry = ListenerRegistry()
    listener_id = registry.add(("p",), lambda _s: _noop())
    await registry.remove(listener_id)
    await registry.remove(listener_id)  # 重复 remove（句柄重复 close）不报错
    await registry.close()
    await registry.close()


async def _noop() -> None:
    pass


async def test_remove_concurrent_with_delivery() -> None:
    """remove（句柄 close）与持续投递并发：取消后绝不再有该监听者的新回调。"""
    registry = ListenerRegistry(callback_timeout=0.1)
    active = 0
    max_concurrent = 0

    async def collector(samples: tuple[ProtocolSample, ...]) -> None:

        sample = samples[0]
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
            registry.offer((_sample("p", float(i)),))
            await asyncio.sleep(0)

    offer_task = asyncio.create_task(offer_loop())
    await asyncio.sleep(0.02)
    await registry.remove(listener_id)
    await offer_task
    assert active == 0
    assert max_concurrent == 1  # 单监听者回调从不并发
    await registry.close()


async def test_remove_concurrent_with_close() -> None:
    registry = ListenerRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(_samples: tuple[ProtocolSample, ...]) -> None:
        entered.set()
        await release.wait()

    listener_id = registry.add((), slow)
    registry.offer((_sample("p1"),))
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
    registry.offer((_sample("p1"),))  # 关闭后投递直接丢弃
    await asyncio.sleep(0.02)
    assert received == []
    with pytest.raises(RuntimeError, match="closed"):
        registry.add((), lambda s: _append(received, s))


async def _append(target: list[str], samples: tuple[ProtocolSample, ...]) -> None:
    target.extend(sample.point_id for sample in samples)


async def test_close_leaves_no_residual_worker_or_callback_tasks() -> None:
    registry = ListenerRegistry(callback_timeout=0.05)
    slow_started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_samples: tuple[ProtocolSample, ...]) -> None:
        slow_started.set()
        await release.wait()

    registry.add((), slow)
    registry.offer((_sample("p1"),))
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


def _residual_listener_tasks() -> list[asyncio.Task[None]]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and "iec104-listen" in (task.get_name() or "")
    ]


def _stubborn_callback() -> tuple[
    ProtocolListenerCallback, asyncio.Event, asyncio.Event
]:
    """吞掉 CancelledError 持续阻塞，直到 release 置位才结束的不可协作回调。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def stubborn(_samples: tuple[ProtocolSample, ...]) -> None:
        entered.set()
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()

    return stubborn, entered, release


async def test_close_timeout_keeps_unfinished_callback_tracked() -> None:
    """测试 A：首次关闭超时后注册表停用，但未完成回调仍受跟踪。"""
    registry = ListenerRegistry(callback_timeout=1.0)
    stubborn, entered, release = _stubborn_callback()
    registry.add((), stubborn)
    registry.offer((_sample("p1"),))
    await entered.wait()

    with pytest.raises(TimeoutError):
        await registry.close(timeout=0.05)

    # 已停止接受新监听者与新数据。
    with pytest.raises(RuntimeError, match="closed"):
        registry.add((), stubborn)
    assert registry.active is False
    registry.offer((_sample("p2"),))
    assert registry._queue.qsize() == 0
    # 未完成回调任务仍被注册表持久跟踪。
    assert len(registry._unresolved) == 1
    tracked = next(iter(registry._unresolved))
    assert not tracked.done()

    release.set()
    await asyncio.wait_for(registry.close(), timeout=1.0)


async def test_repeated_close_succeeds_after_tracked_task_finishes() -> None:
    """测试 B：超时后放行回调，再次 close 成功；第三次安全返回。"""
    registry = ListenerRegistry(callback_timeout=1.0)
    stubborn, entered, release = _stubborn_callback()
    registry.add((), stubborn)
    registry.offer((_sample("p1"),))
    await entered.wait()

    with pytest.raises(TimeoutError):
        await registry.close(timeout=0.05)
    tracked = next(iter(registry._unresolved))

    release.set()
    await asyncio.wait_for(tracked, timeout=1.0)
    await asyncio.wait_for(registry.close(), timeout=1.0)

    assert registry._unresolved == set()
    assert _residual_listener_tasks() == []
    await registry.close()  # 第三次调用安全返回


async def test_repeated_close_times_out_again_while_callback_still_running() -> None:
    """测试 C：回调仍拒绝结束时，再次 close 必须再次超时而非虚报成功。"""
    registry = ListenerRegistry(callback_timeout=1.0)
    stubborn, entered, release = _stubborn_callback()
    registry.add((), stubborn)
    registry.offer((_sample("p1"),))
    await entered.wait()

    with pytest.raises(TimeoutError):
        await registry.close(timeout=0.05)
    tracked = next(iter(registry._unresolved))

    with pytest.raises(TimeoutError):
        await registry.close(timeout=0.05)
    assert not tracked.done()
    assert tracked in registry._unresolved  # 引用没有丢失

    release.set()
    await asyncio.wait_for(registry.close(), timeout=1.0)
    assert _residual_listener_tasks() == []


async def test_concurrent_close_both_succeed_when_resources_converge() -> None:
    """测试 D1：并发关闭、协作回调——两者都成功，无重复清理、无死锁。"""
    registry = ListenerRegistry(callback_timeout=1.0)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cooperative(_samples: tuple[ProtocolSample, ...]) -> None:
        entered.set()
        await release.wait()

    registry.add((), cooperative)
    registry.offer((_sample("p1"),))
    await entered.wait()

    first = asyncio.create_task(registry.close())
    second = asyncio.create_task(registry.close())
    await asyncio.sleep(0.05)  # 等首个关闭进入收敛等待
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2.0)
    assert _residual_listener_tasks() == []
    await registry.close()


async def test_concurrent_close_both_fail_while_callback_unconverged() -> None:
    """测试 D2：回调不可协作时，两个并发关闭者都必须得到 TimeoutError。"""
    registry = ListenerRegistry(callback_timeout=1.0)
    stubborn, entered, release = _stubborn_callback()
    registry.add((), stubborn)
    registry.offer((_sample("p1"),))
    await entered.wait()

    first = asyncio.create_task(registry.close(timeout=0.05))
    second = asyncio.create_task(registry.close(timeout=0.05))
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), timeout=2.0
    )
    assert len([exc for exc in results if isinstance(exc, TimeoutError)]) == 2
    assert len(registry._unresolved) == 1

    release.set()
    await asyncio.wait_for(registry.close(), timeout=1.0)
    assert _residual_listener_tasks() == []


async def test_cancelling_one_closer_keeps_other_closer_on_track() -> None:
    """测试 E：取消一个关闭等待者不破坏任务追踪，另一关闭者仍能收敛资源。"""
    registry = ListenerRegistry(callback_timeout=1.0)
    stubborn, entered, release = _stubborn_callback()
    registry.add((), stubborn)
    registry.offer((_sample("p1"),))
    await entered.wait()

    first = asyncio.create_task(registry.close())
    await asyncio.sleep(0.05)  # 等首个关闭进入收敛等待
    second = asyncio.create_task(registry.close())
    await asyncio.sleep(0.02)  # 第二关闭者在锁上排队

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    tracked = next(iter(registry._unresolved))
    assert not tracked.done()  # 外层取消未取消底层回调

    release.set()
    await asyncio.wait_for(second, timeout=2.0)
    assert registry._unresolved == set()
    assert _residual_listener_tasks() == []
    await registry.close()


async def test_close_cancels_in_flight_callback_and_waits_for_cleanup() -> None:
    registry = ListenerRegistry()
    cleanup_done = asyncio.Event()
    entered = asyncio.Event()

    async def with_cleanup(_samples: tuple[ProtocolSample, ...]) -> None:
        entered.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cleanup_done.set()
            raise

    registry.add((), with_cleanup)
    registry.offer((_sample("p1"),))
    await entered.wait()
    await asyncio.wait_for(registry.close(), timeout=1.0)
    assert cleanup_done.is_set()
