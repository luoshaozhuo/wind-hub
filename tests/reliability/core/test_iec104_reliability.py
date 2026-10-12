"""Core IEC104Driver 可靠性补充矩阵（真实 c104 loopback）。

建连失败重试、断线、自动重连与补召、命令超时已由
``tests/integration/protocols/iec104`` 覆盖；本文件补齐
连接拒绝的显式失败、重复断开与主动读中取消的资源语义。
"""

from __future__ import annotations

import asyncio

import c104
import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import ProtocolSample
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.iec104 import IEC104Driver
from core.infrastructure.protocol.iec104.mapping import IEC104Point, iec104_point
from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.process import free_port
from tests.support.units import NONE_UNIT

pytestmark = pytest.mark.real_service


def _driver(port: int) -> IEC104Driver:
    point = Point(
        point_id="meas",
        business_point_id=BusinessPointId("meas"),
        source_unit=NONE_UNIT,
        access=PointAccess.READ,
        ext={"ioa": 100, "type_id": "M_ME_NC_1"},
    )
    return IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        PointTable(PointTableId("pt"), Protocol("iec104"), {"meas": point}),
        {"common_addr": 1, "t1": 1.0, "t2": 1.0, "t3": 2.0},
    )


async def test_connect_refused_fails_explicitly() -> None:
    driver = _driver(free_port())
    with pytest.raises(ProtocolError):
        await driver.connect()
    assert driver.is_open() is False
    await driver.close()
    await driver.close()


async def test_double_disconnect_while_connected_is_safe() -> None:
    server = IEC104MockServer(port=free_port())
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    try:
        assert driver.is_open()
        await driver.close()
        assert driver.is_open() is False
        await driver.close()
        assert driver.is_open() is False
    finally:
        await server.stop()


async def test_cancel_during_active_read_leaves_no_pending_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主动 C_RD 读在途取消：不得挂起，也不得残留 per-IOA future 泄漏。"""
    server = IEC104MockServer(port=free_port())
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    try:
        # 让发送成功但响应永不到达，读停留在等待 REQUEST 的在途状态。
        async def no_response(mapped: IEC104Point) -> None:
            return None

        monkeypatch.setattr(driver, "_send_read", no_response)
        task = asyncio.create_task(driver.read_one("meas"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # per-IOA future 必须随取消释放，不得泄漏。
        assert 100 not in driver._active_reads
        assert 100 not in driver._active_read_locks
    finally:
        await driver.close()
        await server.stop()


async def test_disconnect_during_pending_active_read_rejects_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在途主动读期间服务端断线：读被显式拒绝，不残留 future/锁引用。"""
    server = IEC104MockServer(port=free_port())
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    try:
        # 让发送成功但响应永不到达，读确定性停留在等待 REQUEST 的在途状态；
        # 故障注入本身是真实的（停止 c104 server，协议栈检测断线）。
        async def no_response(mapped: IEC104Point) -> None:
            return None

        monkeypatch.setattr(driver, "_send_read", no_response)
        task = asyncio.create_task(driver.read_one("meas"))
        await asyncio.sleep(0.05)
        assert 100 in driver._active_reads

        await server.stop()
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(task, timeout=5.0)
        assert 100 not in driver._active_reads
        assert 100 not in driver._active_read_locks
        assert driver.is_open() is False
    finally:
        await driver.close()


async def test_subscriber_callback_exception_does_not_stop_delivery() -> None:
    """订阅回调抛异常被记录后，后续样本仍继续分发，订阅不被静默摘除。"""
    server = IEC104ControlServer(data_points={100: 1500.5})
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    received: list[ProtocolSample] = []
    second_delivered = asyncio.Event()
    calls = 0

    async def flaky_callback(sample: ProtocolSample) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("subscriber bug")
        received.append(sample)
        second_delivered.set()

    try:
        subscription = await driver.subscribe((), flaky_callback)
        station_point = server._station.get_point(100)  # type: ignore[union-attr]
        assert station_point is not None
        station_point.value = 1600.0
        assert station_point.transmit(c104.Cot.SPONTANEOUS)
        station_point.value = 1700.0
        assert station_point.transmit(c104.Cot.SPONTANEOUS)
        station_point.value = 1800.0
        assert station_point.transmit(c104.Cot.SPONTANEOUS)

        await asyncio.wait_for(second_delivered.wait(), timeout=5.0)
        # 同点慢回调按最新样本合并：首样本抛异常后，最新值仍被分发。
        assert received[-1].value == pytest.approx(1800.0)
        assert calls >= 2
        await subscription.close()
    finally:
        await driver.close()
        await server.stop()


async def test_many_dynamic_ioa_reads_leave_stable_resources() -> None:
    """大量动态 IOA 主动读：全部真实读回，锁表/future 表不残留、不无界增长。"""
    data_points = {1000 + index: float(index) for index in range(50)}
    server = IEC104MockServer(port=free_port(), data_points=data_points)
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    try:
        for index in range(50):
            ioa = 1000 + index
            sample = await driver.read_one(iec104_point(f"dyn{ioa}", ioa=ioa, type_id="M_ME_NC_1"))
            assert sample.value == pytest.approx(float(index))
        assert not driver._active_read_locks
        assert not driver._active_reads
        assert not driver._command_locks
        # 动态接收关联按使用过的 IOA 有界登记。
        assert len(driver._dynamic_points) == 50
    finally:
        await driver.close()
        await server.stop()
