"""_KeyLocks：同址串行、异址并行、引用归零后字典无残留（含取消路径）。"""

from __future__ import annotations

import asyncio
import contextlib

from core.infrastructure.protocol.iec104.driver import _KeyLocks


async def test_same_key_is_serialized() -> None:
    locks = _KeyLocks()
    active = 0
    max_active = 0

    async def critical() -> None:
        nonlocal active, max_active
        async with locks.locked("addr"):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(critical() for _ in range(5)))
    assert max_active == 1
    assert locks._locks == {} and locks._refs == {}


async def test_different_keys_run_in_parallel() -> None:
    locks = _KeyLocks()
    active = 0
    max_active = 0

    async def critical(key: int) -> None:
        nonlocal active, max_active
        async with locks.locked(key):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(critical(i) for i in range(5)))
    assert max_active == 5
    assert locks._locks == {} and locks._refs == {}


async def test_cancelled_waiter_releases_refcount() -> None:
    locks = _KeyLocks()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with locks.locked("addr"):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with locks.locked("addr"):
            pass

    holder_task = asyncio.create_task(holder())
    await entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)
    assert locks._refs["addr"] == 2
    waiter_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter_task
    assert locks._refs["addr"] == 1
    release.set()
    await holder_task
    assert locks._locks == {} and locks._refs == {}
