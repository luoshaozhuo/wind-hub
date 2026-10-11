"""IEC104 per-IOA 锁并发正确性与生命周期测试。

验证同 IOA 操作严格互斥、不同 IOA 并行、取消/超时/异常路径释放引用，
以及动态 IOA 高频操作后锁表不无界增长。所有竞争通过事件与 future
确定性构造，不依赖 sleep 碰运气。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application import ProtocolError, ProtocolSample
from core.infrastructure.protocol.iec104.driver import IEC104Driver, _SubscriptionRegistry
from core.infrastructure.protocol.iec104.mapping import iec104_point


class _Station:
    def __init__(self, *, accepted: bool = True) -> None:
        self.points: dict[int, Any] = {}
        self.accepted = accepted

    def get_point(self, ioa: int) -> Any:
        return self.points.get(ioa)

    def add_point(self, *, io_address: int, type: Any) -> Any:  # noqa: A002
        point = _C104Point(type, accepted=self.accepted)
        self.points[io_address] = point
        return point


class _C104Point:
    def __init__(self, point_type: Any, *, accepted: bool = True) -> None:
        self.type = point_type
        self.accepted = accepted
        self.transmit_calls = 0

    def transmit(self, cot: Any) -> bool:
        self.transmit_calls += 1
        return self.accepted

    def on_receive(self, *, callable_: Any = None, **kwargs: Any) -> None:
        del callable_, kwargs


def _driver(t1: float = 0.2) -> IEC104Driver:
    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._closed = False
    driver._station = _Station()
    driver._points_by_id = {}
    driver._points_by_ioa = {}
    driver._dynamic_points = {}
    driver._command_locks = {}
    driver._active_reads = {}
    driver._active_read_locks = {}
    driver._receive_callback_factory = None
    driver._subscriptions = _SubscriptionRegistry()

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.t1 = t1
    driver._config = cfg
    return driver


def _sample(point_id: str, value: float) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=value,
        timestamp=datetime.now(UTC),
    )


class _ReadGate:
    """确定性单点读控制器：send 进入临界区即记录，future 由测试逐个完成。"""

    def __init__(self, driver: IEC104Driver) -> None:
        self.events: list[tuple[str, int]] = []
        self.entered: dict[int, asyncio.Event] = {}

        async def send(mapped: Any) -> None:
            self.events.append(("start", mapped.ioa))
            self.entered.setdefault(mapped.ioa, asyncio.Event()).set()

        driver._send_read = send

    def complete(self, driver: IEC104Driver, ioa: int, value: float = 1.0) -> None:
        driver._active_reads[ioa].set_result(_sample(f"p{ioa}", value))

    def finish(self, ioa: int) -> None:
        self.events.append(("end", ioa))

    async def read(self, driver: IEC104Driver, ioa: int) -> ProtocolSample:
        result = await driver.read_one(iec104_point(f"p{ioa}", ioa=ioa, type_id="M_ME_NC_1"))
        self.finish(ioa)
        return result


@pytest.mark.asyncio
async def test_same_ioa_three_coroutines_serialize() -> None:
    driver = _driver()
    gate = _ReadGate(driver)

    first = asyncio.create_task(gate.read(driver, 11))
    await gate.entered[11].wait() if 11 in gate.entered else None
    # 第一个协程已进入临界区后再启动竞争者
    while ("start", 11) not in gate.events:
        await asyncio.sleep(0)
    second = asyncio.create_task(gate.read(driver, 11))
    third = asyncio.create_task(gate.read(driver, 11))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gate.events == [("start", 11)]

    gate.complete(driver, 11, 1.0)
    await first
    # 依次放行后续等待者，每次只有一个进入临界区
    for value in (2.0, 3.0):
        while len([e for e in gate.events if e == ("start", 11)]) < int(value):
            await asyncio.sleep(0)
        gate.complete(driver, 11, value)
    await second
    await third

    assert gate.events == [("start", 11), ("end", 11)] * 3
    assert 11 not in driver._active_read_locks
    assert not driver._active_reads


@pytest.mark.asyncio
async def test_different_ioa_run_in_parallel() -> None:
    driver = _driver()
    gate = _ReadGate(driver)

    first = asyncio.create_task(gate.read(driver, 11))
    while ("start", 11) not in gate.events:
        await asyncio.sleep(0)
    second = asyncio.create_task(gate.read(driver, 22))
    while ("start", 22) not in gate.events:
        await asyncio.sleep(0)
    # 两个 IOA 同时处于临界区内
    assert gate.events == [("start", 11), ("start", 22)]

    gate.complete(driver, 22)
    gate.complete(driver, 11)
    await first
    await second
    assert not driver._active_read_locks


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_lock_releases_reference() -> None:
    driver = _driver()
    gate = _ReadGate(driver)

    holder = asyncio.create_task(gate.read(driver, 11))
    while ("start", 11) not in gate.events:
        await asyncio.sleep(0)
    waiter = asyncio.create_task(gate.read(driver, 11))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    # 持有者不受影响，正常完成后锁表清空
    gate.complete(driver, 11)
    assert (await holder).value == 1.0
    assert 11 not in driver._active_read_locks

    # 后续操作可立即获得新锁
    again = asyncio.create_task(gate.read(driver, 11))
    while len([e for e in gate.events if e == ("start", 11)]) < 2:
        await asyncio.sleep(0)
    gate.complete(driver, 11)
    await again
    assert 11 not in driver._active_read_locks


@pytest.mark.asyncio
async def test_cancel_while_holding_lock_releases_for_next() -> None:
    driver = _driver()
    gate = _ReadGate(driver)

    holder = asyncio.create_task(gate.read(driver, 11))
    while ("start", 11) not in gate.events:
        await asyncio.sleep(0)
    waiter = asyncio.create_task(gate.read(driver, 11))
    await asyncio.sleep(0)

    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    # 等待者获得锁并正常完成
    while len([e for e in gate.events if e == ("start", 11)]) < 2:
        await asyncio.sleep(0)
    gate.complete(driver, 11)
    await waiter
    assert 11 not in driver._active_read_locks
    assert not driver._active_reads


@pytest.mark.asyncio
async def test_timeout_releases_lock_reference() -> None:
    driver = _driver(t1=0.02)

    async def send(mapped: Any) -> None:
        del mapped

    driver._send_read = send
    with pytest.raises(ProtocolError, match="timed out"):
        await driver.read_one(iec104_point("p", ioa=11, type_id="M_ME_NC_1"))
    assert 11 not in driver._active_read_locks
    assert not driver._active_reads


@pytest.mark.asyncio
async def test_send_read_failure_releases_lock_reference() -> None:
    driver = _driver()

    async def send(mapped: Any) -> None:
        raise ProtocolError("send failed")

    driver._send_read = send
    with pytest.raises(ProtocolError, match="send failed"):
        await driver.read_one(iec104_point("p", ioa=11, type_id="M_ME_NC_1"))
    assert 11 not in driver._active_read_locks
    assert not driver._active_reads


@pytest.mark.asyncio
async def test_command_failure_releases_lock_reference() -> None:
    driver = _driver()
    driver._station = _Station(accepted=False)
    point = iec104_point("cmd", ioa=600, type_id="C_SC_NA_1")

    result = await driver.write_one(point, True)
    assert not result.success

    assert 600 not in driver._command_locks


@pytest.mark.asyncio
async def test_command_type_error_releases_lock_reference() -> None:
    driver = _driver()
    point = iec104_point("cmd", ioa=600, type_id="C_SC_NA_1")

    result = await driver.write_one(point, "not-a-bool")  # type: ignore[arg-type]
    assert not result.success

    assert 600 not in driver._command_locks


@pytest.mark.asyncio
async def test_many_dynamic_ioas_leave_empty_lock_tables() -> None:
    driver = _driver()

    async def send(mapped: Any) -> None:
        driver._active_reads[mapped.ioa].set_result(_sample(mapped.point_id, 1.0))

    driver._send_read = send
    points = [iec104_point(f"d{ioa}", ioa=ioa, type_id="M_ME_NC_1") for ioa in range(1000, 1300)]
    results = await asyncio.gather(*(driver.read_one(point) for point in points))
    assert len(results) == 300
    assert not driver._active_read_locks
    assert not driver._active_reads


@pytest.mark.asyncio
async def test_high_frequency_same_ioa_reads_never_overlap() -> None:
    driver = _driver()
    active = 0
    max_active = 0

    async def send(mapped: Any) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        driver._active_reads[mapped.ioa].set_result(_sample(mapped.point_id, 1.0))

    driver._send_read = send
    point = iec104_point("p", ioa=11, type_id="M_ME_NC_1")
    results = await asyncio.gather(*(driver.read_one(point) for _ in range(50)))
    assert len(results) == 50
    assert max_active == 1
    assert 11 not in driver._active_read_locks


@pytest.mark.asyncio
async def test_high_frequency_same_ioa_writes_never_overlap() -> None:
    driver = _driver()
    point = iec104_point("cmd", ioa=600, type_id="C_SC_NA_1")
    results = await asyncio.gather(*(driver.write_one(point, True) for _ in range(50)))
    assert all(result.success for result in results)
    assert 600 not in driver._command_locks
