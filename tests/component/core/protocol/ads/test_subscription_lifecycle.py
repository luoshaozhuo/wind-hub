"""ADS 区块通知订阅生命周期测试（Component，fake pyads）。

当前架构：所有订阅复用 Driver 的单条 pyads 连接，一个订阅按区块注册
若干 Device Notification，一个有界队列 + 一个 Worker 顺序投递。
本模块覆盖真实 TwinCAT 不可用时仍可确定性验证的行为：

- 同一连接上多个订阅相互独立，关闭一个不影响另一个；
- 用户回调异常不终止 Worker；
- 关闭订阅注销其全部 notification handle，但不关闭共享连接；
- start 第二区块注册失败时清理已注册 handle，Driver 不跟踪失败订阅；
- 注销命令抛错只记录日志，不挂住关闭流程；
- 旧一代原生回调在订阅关闭/重建后静默丢弃。

内部 watchdog、每订阅独立连接等旧架构行为已删除，不再测试。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from typing import Any

import pytest

import core.infrastructure.protocol.ads.subscription as subscription_module
from core.application import ProtocolSample
from core.application.errors import ProtocolError
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.units import NONE_UNIT


def _point(point_id: str, *, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "REAL", "index_group": 0x4020, "index_offset": offset},
    )


def _driver(*points: Point) -> ADSDriver:
    table = PointTable(PointTableId("ads_pt"), Protocol("ads"), {p.point_id: p for p in points})
    driver = ADSDriver(ConnectionEndpoint("192.0.2.20", 801), table, {})
    driver._connected = True
    return driver


def _wrap(driver: ADSDriver, connection: _Connection) -> None:
    from core.infrastructure.protocol.ads.session import _AdsSession

    driver._session = _AdsSession(connection)


class _Attrib:
    def __init__(
        self, length: int, *, trans_mode: int, cycle_time: float, max_delay: float = 0.0
    ) -> None:
        self.length = length
        self.trans_mode = trans_mode
        self.cycle_time = cycle_time
        self.max_delay = max_delay


class _Connection:
    """记录 notification 注册/注销的单连接 fake（共享连接模型）。"""

    def __init__(self) -> None:
        self.is_open = True
        self.closed = False
        self.callbacks: dict[int, Callable[..., None]] = {}
        self.deleted: list[object] = []
        self._next_handle = 1
        self.fail_registration_after: int | None = None

    def notification(self, datatype: object) -> Callable[..., Callable[..., None]]:
        del datatype

        def decorator(callback: Callable[..., None]) -> Callable[..., None]:
            return callback

        return decorator

    def add_device_notification(
        self,
        address: tuple[int, int],
        attr: object,
        callback: Callable[..., None],
    ) -> tuple[object, object]:
        del address, attr
        if (
            self.fail_registration_after is not None
            and len(self.callbacks) >= self.fail_registration_after
        ):
            raise ConnectionError("registration rejected")
        handle = self._next_handle
        self._next_handle += 1
        self.callbacks[handle] = callback
        return (handle, None)

    def del_device_notification(self, handle: object, user_handle: object) -> None:
        del user_handle
        if not isinstance(handle, int):
            raise AssertionError("notification handle must be registered int")
        self.callbacks.pop(handle, None)
        self.deleted.append(handle)

    def fire(self, handle: int, value: float, *, timestamp: Any = None) -> None:
        callback = self.callbacks[handle]
        callback(handle, (0x4020, 0), timestamp, ctypes_ubyte_4(value))


class _FakePyads:
    ADSTRANS_SERVERCYCLE = 3
    Connection = object
    NotificationAttrib = _Attrib


def ctypes_ubyte_4(value: float) -> object:
    from ctypes import c_ubyte

    return (c_ubyte * 4)(*struct.pack("<f", value))


@pytest.fixture
def fake_pyads(monkeypatch: pytest.MonkeyPatch) -> _Connection:
    connection = _Connection()
    fake = _FakePyads()
    monkeypatch.setattr(subscription_module, "pyads", fake)
    return connection


@pytest.mark.asyncio
async def test_multiple_subscriptions_share_one_connection_and_are_independent(
    fake_pyads: _Connection,
) -> None:
    driver = _driver(_point("speed", offset=10))
    _wrap(driver, fake_pyads)
    first_samples: list[ProtocolSample] = []
    second_samples: list[ProtocolSample] = []
    second_received = asyncio.Event()

    async def first_cb(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            first_samples.append(sample)

    async def second_cb(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            second_samples.append(sample)
            second_received.set()

    first = await driver.subscribe(("speed",), first_cb, interval=0.25)
    second = await driver.subscribe(("speed",), second_cb, interval=0.25)
    # 共享连接：两个订阅都注册在 driver 的同一条连接上。
    assert driver._session is not None
    assert set(fake_pyads.callbacks) == {1, 2}

    await first.close()
    assert first._closed
    fake_pyads.fire(2, 3.0)
    await asyncio.wait_for(second_received.wait(), timeout=1.0)
    assert [sample.value for sample in second_samples] == [3.0]
    assert first_samples == []
    await second.close()
    # 订阅关闭不关闭共享业务连接。
    assert fake_pyads.is_open


@pytest.mark.asyncio
async def test_callback_exception_does_not_stop_subscription(
    fake_pyads: _Connection,
) -> None:
    driver = _driver(_point("speed", offset=10))
    _wrap(driver, fake_pyads)
    received: list[float] = []
    delivered = asyncio.Event()
    calls = 0

    async def flaky(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("subscriber bug")
            assert sample.value is not None
            received.append(float(sample.value))
            delivered.set()

    handle = await driver.subscribe(("speed",), flaky, interval=0.25)
    try:
        fake_pyads.fire(1, 1.0)
        fake_pyads.fire(1, 2.0)
        await asyncio.wait_for(delivered.wait(), timeout=1.0)
        assert received == [2.0]
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_close_deregisters_all_handles_and_is_idempotent(
    fake_pyads: _Connection,
) -> None:
    driver = _driver(_point("speed", offset=10), _point("torque", offset=1100))
    _wrap(driver, fake_pyads)
    handle = await driver.subscribe((), _noop, interval=0.25)
    assert len(fake_pyads.callbacks) == 2

    await handle.close()
    assert fake_pyads.deleted == [1, 2]
    assert fake_pyads.callbacks == {}
    assert fake_pyads.is_open  # 共享连接保持打开
    await handle.close()
    assert fake_pyads.deleted == [1, 2]


async def _noop(samples: tuple[ProtocolSample, ...]) -> None:
    for sample in samples:
        del sample


@pytest.mark.asyncio
async def test_start_failure_deregisters_partial_blocks_and_is_not_tracked(
    fake_pyads: _Connection,
) -> None:
    """第二个区块注册失败：已注册 handle 被清理，异常上抛，Driver 不跟踪。"""
    driver = _driver(_point("speed", offset=10), _point("torque", offset=1100))
    _wrap(driver, fake_pyads)
    fake_pyads.fail_registration_after = 1

    # 传输级注册失败与读写路径同口径归一化为 ProtocolError，原始异常保留为 __cause__。
    with pytest.raises(ProtocolError, match="ADS subscribe failed") as exc_info:
        await driver.subscribe(("speed", "torque"), _noop, interval=0.25)
    assert isinstance(exc_info.value.__cause__, ConnectionError)

    assert fake_pyads.deleted == [1]
    assert driver._subscriptions == set()
    assert driver.is_open()


@pytest.mark.asyncio
async def test_deregistration_failure_does_not_block_close(
    fake_pyads: _Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    driver = _driver(_point("speed", offset=10))
    _wrap(driver, fake_pyads)

    def raise_on_delete(handle: object, user_handle: object) -> None:
        raise ConnectionError("deregistration lost")

    monkeypatch.setattr(fake_pyads, "del_device_notification", raise_on_delete)
    handle = await driver.subscribe(("speed",), _noop, interval=0.25)

    await asyncio.wait_for(handle.close(), timeout=1.0)
    assert handle._closed
    assert "deregistration failed" in caplog.text


@pytest.mark.asyncio
async def test_old_generation_native_callback_is_silently_dropped(
    fake_pyads: _Connection,
) -> None:
    """订阅关闭后再订阅：旧订阅残留的原生回调不得再投递样本。"""
    driver = _driver(_point("speed", offset=10))
    _wrap(driver, fake_pyads)
    received: list[float] = []

    async def collect(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            received.append(float(sample.value))  # type: ignore[arg-type]

    first = await driver.subscribe(("speed",), collect, interval=0.25)
    old_callback = fake_pyads.callbacks[1]
    await first.close()

    second = await driver.subscribe(("speed",), collect, interval=0.25)
    try:
        # 旧一代回调（携带旧 generation 闭包）：静默丢弃。
        old_callback(1, (0x4020, 10), None, ctypes_ubyte_4(1.0))
        await asyncio.sleep(0.05)
        assert received == []
        # 新一代回调正常投递。
        fake_pyads.fire(2, 2.0)
        for _ in range(20):
            await asyncio.sleep(0)
            if received:
                break
        assert received == [2.0]
    finally:
        await second.close()
