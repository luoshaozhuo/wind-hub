"""IEC104 订阅注册表 close barrier 单元测试。

钉住契约：``close()`` 返回后本订阅的 callback 不再开始新的调用，且已
开始的调用都已执行完毕。分发与注销都发生在事件循环线程，无交错窗口。
"""

from __future__ import annotations

import asyncio

import pytest

from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.iec104.driver import _SubscriptionRegistry

pytestmark = pytest.mark.asyncio


def _pv(point_id: str = "p1") -> PointValue:
    return PointValue(device_id="d1", point_id=point_id, value=1.0)


def _registry_with(points: list, callback) -> tuple:
    registry = _SubscriptionRegistry()
    handle = registry.subscribe(points, callback, lambda ref: 100)
    return registry, handle


async def test_close_waits_for_in_flight_callback() -> None:
    """close 阻塞直到已开始的 callback 完成；完成后 close 才返回。"""
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[float] = []

    async def cb(pv: PointValue) -> None:
        started.set()
        await release.wait()
        calls.append(pv.value)

    registry, handle = _registry_with([], cb)
    await registry.dispatch(_pv(), 100)
    await started.wait()

    closer = asyncio.ensure_future(handle.close())
    await asyncio.sleep(0)
    assert not closer.done(), "close 必须等待在途 callback"

    release.set()
    await closer
    assert calls == [1.0]

    # close 返回后再分发：不再触达本订阅。
    await registry.dispatch(_pv(), 100)
    await asyncio.sleep(0)
    assert calls == [1.0]


async def test_close_before_dispatch_task_runs_skips_delivery() -> None:
    """dispatch 任务尚未运行时 close：快照不含已关闭订阅，零投递。"""
    calls: list[float] = []

    async def cb(pv: PointValue) -> None:
        calls.append(pv.value)

    registry, handle = _registry_with([], cb)
    await handle.close()
    await registry.dispatch(_pv(), 100)
    await asyncio.sleep(0)
    assert calls == []


async def test_close_is_idempotent_and_scoped() -> None:
    """重复 close 幂等；关闭一个订阅不影响同 IOA 的其他订阅。"""
    calls_a: list[float] = []
    calls_b: list[float] = []

    async def cb_a(pv: PointValue) -> None:
        calls_a.append(pv.value)

    async def cb_b(pv: PointValue) -> None:
        calls_b.append(pv.value)

    registry = _SubscriptionRegistry()
    handle_a = registry.subscribe([], cb_a, lambda ref: 100)
    registry.subscribe([], cb_b, lambda ref: 100)

    await handle_a.close()
    await handle_a.close()

    await registry.dispatch(_pv(), 100)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls_a == []
    assert calls_b == [1.0]
