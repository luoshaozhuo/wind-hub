"""ADS notification 订阅生命周期测试（Component，fake pyads）。

覆盖真实 TwinCAT 不可用时可验证的行为：同点多订阅相互独立、
用户回调异常不终止订阅、close 释放全部 notification handle 与连接、
start 期间取消清理部分注册、通知失联后 watchdog 重建订阅池。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

import core.infrastructure.protocol.ads.subscription as subscription_module
from core.application import ProtocolSample
from core.domain import (
    UNIT_CATALOG,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UnitCode,
)
from core.infrastructure.protocol.ads import ADSDriver


def _point(point_id: str, *, offset: int = 10) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ_WRITE,
        ext={"data_type": "REAL", "index_group": 0x4020, "index_offset": offset},
    )


def _driver(*points: Point) -> ADSDriver:
    table = PointTable("ads_pt", Protocol("ads"), {p.point_id: p for p in points})
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, {})
    driver._connected = True
    return driver


class _Attrib:
    def __init__(
        self,
        length: int,
        *,
        cycle_time: float,
        max_delay: float,
        trans_mode: int,
    ) -> None:
        self.length = length
        self.cycle_time = cycle_time
        self.max_delay = max_delay
        self.trans_mode = trans_mode


class _Connection:
    """记录 notification 注册/注销的最小 ADS 连接 fake。"""

    instances: list[_Connection] = []
    def __init__(self, net_id: object, port: int, host: str) -> None:
        del net_id, port, host
        self.is_open = False
        self.callbacks: dict[int, Callable[..., None]] = {}
        self.deleted: list[int] = []
        self._next_handle = 0
        type(self).instances.append(self)

    def set_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read_state(self) -> tuple[int, int]:
        return (5, 0)

    def notification(self, datatype: object) -> Callable[..., None]:
        del datatype
        return lambda callback: callback

    def add_device_notification(
        self,
        address: tuple[int, int],
        attr: object,
        callback: Callable[..., None],
    ) -> tuple[object, object]:
        handle = (address, self._next_handle)
        self._next_handle += 1
        self.callbacks[self._next_handle] = callback
        return (handle, None)

    def del_device_notification(self, handle: object, user_handle: object) -> None:
        del user_handle
        self.deleted.append(handle)

    def fire(self, value: float) -> None:
        for callback in self.callbacks.values():
            callback(None, (0x4020, 10), None, value)


class _FakePyads:
    ADSTRANS_SERVERCYCLE = 3
    PLCTYPE_REAL = object()
    Connection = _Connection
    NotificationAttrib = _Attrib


@pytest.fixture
def fake_pyads(monkeypatch: pytest.MonkeyPatch) -> type[_Connection]:
    _Connection.instances = []
    monkeypatch.setattr(subscription_module, "pyads", _FakePyads)
    return _Connection


@pytest.mark.asyncio
async def test_same_point_multiple_subscriptions_are_independent(
    fake_pyads: type[_Connection],
) -> None:
    driver = _driver(_point("speed"))
    first_samples: list[ProtocolSample] = []
    second_samples: list[ProtocolSample] = []
    second_received = asyncio.Event()

    async def first_cb(sample: ProtocolSample) -> None:
        first_samples.append(sample)

    async def second_cb(sample: ProtocolSample) -> None:
        second_samples.append(sample)
        second_received.set()

    first = await driver.subscribe(("speed",), first_cb, interval=0.25)
    second = await driver.subscribe(("speed",), second_cb, interval=0.25)
    assert len(fake_pyads.instances) == 2

    await first.close()
    fake_pyads.instances[1].fire(3.0)
    await asyncio.wait_for(second_received.wait(), timeout=1.0)
    assert [sample.value for sample in second_samples] == [3.0]
    assert first_samples == []
    await second.close()


@pytest.mark.asyncio
async def test_callback_exception_does_not_stop_subscription(
    fake_pyads: type[_Connection],
) -> None:
    driver = _driver(_point("speed"))
    received: list[float] = []
    delivered = asyncio.Event()
    calls = 0

    async def flaky(sample: ProtocolSample) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("subscriber bug")
        received.append(float(sample.value))
        delivered.set()

    handle = await driver.subscribe(("speed",), flaky, interval=0.25)
    connection = fake_pyads.instances[0]
    connection.fire(1.0)
    connection.fire(2.0)
    await asyncio.wait_for(delivered.wait(), timeout=1.0)
    assert received == [2.0]
    await handle.close()


@pytest.mark.asyncio
async def test_close_releases_all_notification_handles(
    fake_pyads: type[_Connection],
) -> None:
    driver = _driver(_point("speed", offset=10), _point("torque", offset=20))
    handle = await driver.subscribe((), _noop, interval=0.25)
    connection = fake_pyads.instances[0]
    assert len(connection.callbacks) == 2

    await handle.close()
    assert len(connection.deleted) == 2
    assert connection.is_open is False
    # 重复 close 安全且不重复注销
    await handle.close()
    assert len(connection.deleted) == 2


async def _noop(sample: ProtocolSample) -> None:
    del sample


@pytest.mark.asyncio
async def test_cancel_during_start_cleans_partial_registration(
    fake_pyads: type[_Connection],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start 在第二次注册前被取消：已注册的 handle 与连接必须被清理。"""
    driver = _driver(_point("speed", offset=10), _point("torque", offset=20))
    resolve_calls = 0
    entered_second = asyncio.Event()
    release_second = asyncio.Event()

    original_resolve = subscription_module.ADSSubscription._resolve_point

    async def gated_resolve(self: object, connection: object, point: object) -> object:
        nonlocal resolve_calls
        resolve_calls += 1
        if resolve_calls == 2:
            entered_second.set()
            await release_second.wait()
        return await original_resolve(self, connection, point)

    monkeypatch.setattr(
        subscription_module.ADSSubscription,
        "_resolve_point",
        gated_resolve,
    )

    task = asyncio.create_task(driver.subscribe(("speed", "torque"), _noop, interval=0.25))
    await entered_second.wait()
    task.cancel()
    release_second.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    connection = fake_pyads.instances[0]
    # 取消触发 _rebuild_locked 的 BaseException 清理路径：已注册 handle 注销、连接关闭。
    assert len(connection.deleted) == 1
    assert connection.is_open is False


@pytest.mark.asyncio
async def test_watchdog_rebuilds_pool_when_notifications_stall(
    fake_pyads: type[_Connection],
) -> None:
    """通知失联（PLC 不再上送）：watchdog 关闭旧连接并重建订阅池，恢复分发。"""
    driver = _driver(_point("speed"))
    received: list[float] = []
    delivered = asyncio.Event()

    async def on_sample(sample: ProtocolSample) -> None:
        received.append(float(sample.value))
        delivered.set()

    handle = await driver.subscribe(("speed",), on_sample, interval=0.05)
    old_connection = fake_pyads.instances[0]
    old_connection.fire(1.0)
    await asyncio.wait_for(delivered.wait(), timeout=1.0)

    # 不再上送，等待 watchdog 检测到 stalled 并重建（interval + 通知超时）。
    for _ in range(200):
        await asyncio.sleep(0.1)
        if len(fake_pyads.instances) > 1:
            break
    assert len(fake_pyads.instances) > 1, "watchdog did not rebuild the pool"
    new_connection = fake_pyads.instances[-1]
    assert new_connection is not old_connection
    assert old_connection.is_open is False
    assert new_connection.is_open is True
    assert len(new_connection.callbacks) == 1

    delivered.clear()
    new_connection.fire(2.0)
    await asyncio.wait_for(delivered.wait(), timeout=1.0)
    assert received[-1] == 2.0
    await handle.close()
    assert new_connection.is_open is False
