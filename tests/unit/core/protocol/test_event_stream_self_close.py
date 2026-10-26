"""ProtocolEventStream 观察者在自身回调内关闭句柄的生命周期专项。

ROUND2 专项审查复现：回调内自关闭不得留下僵尸观察者任务，且 close() 返回后
不得再调用该观察者的回调（队列中尚未消费的事件应随之丢弃）。
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.protocol_contract import (
    ProtocolOperation,
    ProtocolOperationState,
)
from core.infrastructure.protocol.events import ProtocolEventStream, _Observer

pytestmark = pytest.mark.asyncio


async def test_observer_closing_itself_in_callback_does_not_leak_zombie_task() -> None:
    stream = ProtocolEventStream()
    delivered: list[int] = []

    async def callback(event: object) -> None:
        seq = event.sequence  # type: ignore[attr-defined]
        delivered.append(seq)
        if seq == 2:
            # 模拟观察者在自己的回调内注销自身（上层在回调中决定停止观察）。
            await handle.close()

    handle = stream.observe(callback)
    key = handle._key  # type: ignore[attr-defined]  # noqa: SLF001
    observer: _Observer = stream._observers[key]  # noqa: SLF001

    for seq in range(1, 5):
        stream.emit(
            f"op{seq}",
            ProtocolOperation.READ,
            ProtocolOperationState.RUNNING,
        )

    # 给队列充分排空时间；僵尸任务会一直挂在 queue.get() 上。
    await asyncio.sleep(0.1)

    # close() 后注册表清空。
    assert key not in stream._observers  # noqa: SLF001
    # 期望：观察者任务真正终止，而不是 cancelling()==1 的僵尸任务。
    assert observer.task.done(), "self-closed observer task must terminate, not leak"
    # 自关闭路径不得对自己 cancel：任务正常退出（非 CancelledError、无 cancelling 计数）。
    assert observer.task.cancelling() == 0
    assert observer.task.exception() is None
    # 期望：注销生效后回调不得继续收到队列中剩余事件。
    assert delivered == [1, 2], f"callback invoked after close: {delivered}"


async def test_self_close_then_repeated_external_close_is_idempotent() -> None:
    """自关闭返回后再次 close 必须幂等，且任务保持终止、无异常。"""
    stream = ProtocolEventStream()

    async def callback(event: object) -> None:
        if event.sequence == 1:  # type: ignore[attr-defined]
            await handle.close()

    handle = stream.observe(callback)
    key = handle._key  # type: ignore[attr-defined]  # noqa: SLF001
    observer: _Observer = stream._observers[key]  # noqa: SLF001
    stream.emit("op1", ProtocolOperation.READ, ProtocolOperationState.RUNNING)
    stream.emit("op2", ProtocolOperation.READ, ProtocolOperationState.RUNNING)
    await asyncio.sleep(0.1)

    assert observer.task.done()
    # 自关闭之后由外部重复关闭：不得抛错、不得重新注册观察者。
    await handle.close()
    await handle.close()
    assert key not in stream._observers  # noqa: SLF001
    assert observer.task.done()
    assert observer.task.cancelling() == 0


async def test_self_close_of_one_observer_leaves_others_independent() -> None:
    """一个观察者回调内自关闭不得波及同流上的其他观察者。"""
    stream = ProtocolEventStream()
    delivered_a: list[int] = []
    delivered_b: list[int] = []

    async def callback_a(event: object) -> None:
        seq = event.sequence  # type: ignore[attr-defined]
        delivered_a.append(seq)
        if seq == 2:
            await handle_a.close()

    async def callback_b(event: object) -> None:
        delivered_b.append(event.sequence)  # type: ignore[attr-defined]

    handle_a = stream.observe(callback_a)
    handle_b = stream.observe(callback_b)
    key_a = handle_a._key  # type: ignore[attr-defined]  # noqa: SLF001
    key_b = handle_b._key  # type: ignore[attr-defined]  # noqa: SLF001
    observer_a: _Observer = stream._observers[key_a]  # noqa: SLF001
    observer_b: _Observer = stream._observers[key_b]  # noqa: SLF001

    for seq in range(1, 5):
        stream.emit(
            f"op{seq}",
            ProtocolOperation.READ,
            ProtocolOperationState.RUNNING,
        )
    await asyncio.sleep(0.1)

    # A 在 seq=2 自关闭后不再收到事件；B 独立收齐全部事件且任务存活。
    assert delivered_a == [1, 2]
    assert observer_a.task.done()
    assert key_a not in stream._observers  # noqa: SLF001
    assert delivered_b == [1, 2, 3, 4]
    assert not observer_b.task.done()
    assert key_b in stream._observers  # noqa: SLF001

    # 随后外部关闭 B 正常收敛。
    await handle_b.close()
    assert observer_b.task.done()


async def test_external_close_cancels_observer_cleanly() -> None:
    """对照组：外部任务关闭观察者时行为正常（确认问题仅限自关闭路径）。"""
    stream = ProtocolEventStream()
    delivered: list[int] = []

    async def callback(event: object) -> None:
        delivered.append(event.sequence)  # type: ignore[attr-defined]
        await asyncio.sleep(0)

    handle = stream.observe(callback)
    for seq in range(1, 4):
        stream.emit(
            f"op{seq}",
            ProtocolOperation.READ,
            ProtocolOperationState.RUNNING,
        )
    await asyncio.sleep(0.05)
    await handle.close()
    assert delivered == [1, 2, 3]
    assert handle._key not in stream._observers  # type: ignore[attr-defined]  # noqa: SLF001
