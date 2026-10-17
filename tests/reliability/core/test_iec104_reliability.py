"""Core IEC104Driver 可靠性矩阵（真实 TCP：c104 loopback + 黑洞从站）。

覆盖：连接拒绝显式失败、重复断开、在途读取取消的 future/锁清理、
断线期间在途请求失败、监听回调异常隔离、大量真实主动读后资源归零。
自动重连不属于 Driver 职责（上层显式 connect），由恢复测试覆盖。
"""

from __future__ import annotations

import asyncio
from typing import Any

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
from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.iec104_blackhole import StartdtBlackhole
from tests.support.process import free_port
from tests.support.units import NONE_UNIT

pytestmark = pytest.mark.real_service


def _point(point_id: str, ioa: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ,
        ext={"ioa": ioa, "ca": 1, "type_id": "M_ME_NC_1"},
    )


def _driver(port: int, points: list[Point] | None = None, **options: Any) -> IEC104Driver:
    points = points or [_point("meas", 100)]
    merged: dict[str, Any] = {
        "common_addresses": "1",
        "t0": 3.0,
        "t1": 3.0,
        "t2": 1.0,
        "t3": 2.0,
    }
    merged.update(options)
    return IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        PointTable(PointTableId("pt"), Protocol("iec104"),
                   {point.point_id: point for point in points}),
        merged,
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


async def test_cancel_during_pending_read_leaves_no_residue() -> None:
    """黑洞只回 S 帧：C_RD 永不在途完成；取消后 future 与按址锁必须归零。"""
    blackhole = StartdtBlackhole(free_port())
    await blackhole.start()
    driver = _driver(blackhole.port, t1=10.0)
    await driver.connect()
    try:
        task = asyncio.create_task(driver.read_one("meas"))
        await asyncio.sleep(0.15)
        assert not task.done()
        session = driver._session
        assert session is not None
        assert (1, 100) in session.reads  # 在途读取以完整 (ca,ioa) 登记
        assert (1, 100) in driver._read_locks._refs

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (1, 100) not in session.reads
        assert not driver._read_locks._refs
        assert not driver._read_locks._locks
    finally:
        await driver.close()
        await blackhole.stop()


async def test_disconnect_during_pending_read_rejects_read() -> None:
    """在途读取期间对端断线：读取显式失败，future/锁引用全部释放。"""
    blackhole = StartdtBlackhole(free_port())
    await blackhole.start()
    driver = _driver(blackhole.port, t1=10.0)
    await driver.connect()
    try:
        task = asyncio.create_task(driver.read_one("meas"))
        await asyncio.sleep(0.15)
        assert not task.done()

        await blackhole.stop()
        with pytest.raises(ProtocolError):
            await asyncio.wait_for(task, timeout=5.0)

        assert driver.is_open() is False
        assert not driver._read_locks._refs
        assert not driver._read_locks._locks
    finally:
        await driver.close()


async def test_listener_callback_exception_does_not_stop_delivery() -> None:
    """监听回调抛异常被隔离：后续 spontaneous 样本继续分发给同一监听者。"""
    server = IEC104ControlServer(data_points={100: 1500.5})
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    received: list[float] = []
    third_delivered = asyncio.Event()
    calls = 0

    async def flaky_callback(sample: ProtocolSample) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("listener bug")
        received.append(float(sample.value))  # type: ignore[arg-type]
        if calls == 3:
            third_delivered.set()

    try:
        await driver.listen((), flaky_callback)
        station_point = server._station.get_point(100)  # type: ignore[union-attr]
        assert station_point is not None
        for value in (1600.0, 1700.0, 1800.0):
            station_point.value = value
            assert station_point.transmit(c104.Cot.SPONTANEOUS)
            await asyncio.sleep(0.1)

        await asyncio.wait_for(third_delivered.wait(), timeout=5.0)
        assert calls == 3
        assert received == [1700.0, 1800.0]  # 首样本回调抛异常但不影响投递
    finally:
        await driver.close()
        await server.stop()


async def test_many_active_reads_leave_stable_resources() -> None:
    """50 个配置点全部真实 C_RD 读回：锁表/future 表不残留、不无界增长。"""
    data_points = {1000 + index: float(index) for index in range(50)}
    points = [_point(f"meas{1000 + index}", 1000 + index) for index in range(50)]
    server = IEC104MockServer(port=free_port(), data_points=data_points)
    await server.start()
    driver = _driver(server.port, points=points)
    await driver.connect()
    try:
        for index in range(50):
            sample = await driver.read_one(f"meas{1000 + index}")
            assert sample.value == pytest.approx(float(index), abs=1e-4)

        session = driver._session
        assert session is not None
        assert not session.reads
        assert not session.commands
        assert not session.interrogations
        assert not driver._read_locks._locks
        assert not driver._read_locks._refs
        assert not driver._command_locks._locks
        assert not driver._gi_locks._locks
    finally:
        await driver.close()
        await server.stop()
