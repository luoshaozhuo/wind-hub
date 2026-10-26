"""driver._Listener 的有界队列语义：过滤、溢出失效、回调异常隔离、关闭。"""

from __future__ import annotations

import asyncio

import pytest

from core.infrastructure.protocol.iec104.driver import _Listener
from tests.support.iec104_fake import (
    connect_driver,
    floating_data_msg,
    make_driver,
    make_point,
)

pytestmark = pytest.mark.asyncio


async def _pump(cycles: int = 10) -> None:
    for _ in range(cycles):
        await asyncio.sleep(0)


async def test_listener_filters_other_point_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    p1 = make_point("p1", 100)
    p2 = make_point("p2", 200)
    driver = make_driver([p1, p2])
    conn = await connect_driver(driver, monkeypatch)

    received: list[tuple[object, ...]] = []
    handle = await driver.listen(["p1"], received.append)

    conn.deliver(
        [
            floating_data_msg(1, 100, 1.5),
            floating_data_msg(1, 200, 2.5),
        ]
    )
    await _pump()

    assert len(received) == 1
    (batch,) = received
    assert len(batch) == 1
    assert batch[0].point_id == "p1"
    assert batch[0].value == 1.5

    await handle.close()
    await driver.close()


async def test_listener_overflow_invalidates_and_cancels_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = make_point("wind", 100)
    driver = make_driver([point])
    conn = await connect_driver(driver, monkeypatch)

    gate = asyncio.Event()

    async def blocked_callback(_batch: tuple[object, ...]) -> None:
        await gate.wait()

    handle = await driver.listen(["wind"], blocked_callback)
    assert isinstance(handle, _Listener)

    # 队列容量 256：第 1 批被回调取走后阻塞，再投递 257 批必然溢出。
    for _ in range(258):
        conn.deliver([floating_data_msg(1, 100, 1.0)])
    await asyncio.sleep(0.2)

    assert handle.failed is True
    assert handle.task.done()
    assert handle.task.cancelled()
    gate.set()

    # 失效后的 offer 直接丢弃，不再入队。
    queue_size = handle.queue.qsize()
    handle.offer(("never enqueued",))  # type: ignore[arg-type]
    assert handle.queue.qsize() == queue_size

    await handle.close()
    assert handle not in driver._listeners
    await driver.close()


async def test_listener_callback_exception_invalidates_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = make_point("wind", 100)
    driver = make_driver([point])
    conn = await connect_driver(driver, monkeypatch)

    calls = 0

    async def failing_callback(_batch: tuple[object, ...]) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("consumer exploded")

    handle = await driver.listen(["wind"], failing_callback)

    conn.deliver([floating_data_msg(1, 100, 1.0)])
    await asyncio.sleep(0.1)

    assert handle.failed is True
    assert handle.task.done()
    assert calls == 1

    # 失效后新数据不再触发回调。
    conn.deliver([floating_data_msg(1, 100, 2.0)])
    await asyncio.sleep(0.1)
    assert calls == 1

    await handle.close()
    assert handle not in driver._listeners
    await driver.close()


async def test_listener_close_is_idempotent_and_removes_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = make_point("wind", 100)
    driver = make_driver([point])
    conn = await connect_driver(driver, monkeypatch)

    delivered = asyncio.Event()

    async def callback(batch: tuple[object, ...]) -> None:
        assert batch[0].point_id == "wind"
        delivered.set()

    handle = await driver.listen(["wind"], callback)
    assert handle in driver._listeners

    conn.deliver([floating_data_msg(1, 100, 9.0)])
    await asyncio.wait_for(delivered.wait(), timeout=1)

    await handle.close()
    await handle.close()  # 幂等：不抛错、不重复移除
    assert handle not in driver._listeners
    assert handle.failed is True
    await driver.close()
