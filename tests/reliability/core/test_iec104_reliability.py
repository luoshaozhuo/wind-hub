"""Core IEC104Driver 可靠性矩阵（真实 TCP：c104 loopback + 黑洞从站）。

覆盖：连接拒绝显式失败、重复断开、在途读取取消的 future 清理、
断线期间在途请求失败、监听回调异常隔离（坏监听者失效、不牵连其他监听者）、
大量真实主动读后在途表归零且锁表按配置点有界不随操作增长。
自动重连不属于 Driver 职责（上层显式 connect），由恢复测试覆盖。

clean rewrite 后的资源模型（见 driver.py）：
- 在途读/控/召唤登记在 driver 的 ``_read``/``_control``/``_interrogation``；
  session 只是传输层，不再持有业务 future 表；
- 按 (ca,ioa) 的 ``_locks`` 与按 ca 的 ``_station_locks`` 是 setdefault
  常驻锁，键空间受配置点表有界，同一地址重复操作不得增长；
- 在途读被取消/超时按不确定性隔离会话（fail-closed），物理关闭在
  close() 收敛，不允许留下 iec104-receive 任务。
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
        PointTable(
            PointTableId("pt"),
            Protocol("iec104"),
            {point.point_id: point for point in points},
        ),
        merged,
    )


def _iec104_tasks() -> set[asyncio.Task[Any]]:
    return {
        task
        for task in asyncio.all_tasks()
        if not task.done() and "iec104" in (task.get_name() or "")
    }


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
    """黑洞只回 S 帧：C_RD 永不在途完成；取消后 future 登记必须清除。

    新 driver 对在途读取消按不确定性 fail-closed：会话被隔离并物理关闭，
    close() 收敛后不得残留 iec104 任务。按址锁按配置常驻、不随操作增加。
    """
    blackhole = StartdtBlackhole(free_port())
    await blackhole.start()
    driver = _driver(blackhole.port, t1=10.0)
    await driver.connect()
    try:
        task = asyncio.create_task(driver.read_one("meas"))
        await asyncio.sleep(0.15)
        assert not task.done()
        # 在途读取以完整 (ca,ioa) 登记在 driver 业务表。
        assert (1, 100) in driver._read
        assert (1, 100) in driver._locks

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (1, 100) not in driver._read
    finally:
        await driver.close()
        await blackhole.stop()

    # 物理关闭收敛后无在途表残留、无 iec104 后台任务。
    assert not driver._read
    assert not driver._control
    assert not driver._interrogation
    assert not _iec104_tasks()


async def test_disconnect_during_pending_read_rejects_read() -> None:
    """在途读取期间对端断线：读取显式失败，future 登记释放，会话关闭。"""
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
        assert not driver._read
    finally:
        await driver.close()

    assert not _iec104_tasks()


async def test_listener_callback_exception_invalidates_only_bad_listener() -> None:
    """监听回调抛异常的新契约：坏监听者被失效，其他监听者继续收数。

    driver 的 _Listener 在回调异常时 fail-closed 置 failed 并结束派发任务，
    避免静默丢数据；同一 driver 上的健康监听者不得被牵连。
    """
    server = IEC104ControlServer(data_points={100: 1500.5})
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    good_received: list[float] = []
    flaky_batches = 0
    good_done = asyncio.Event()

    async def good_callback(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            good_received.append(float(sample.value))  # type: ignore[arg-type]
        if len(good_received) == 3:
            good_done.set()

    async def flaky_callback(samples: tuple[ProtocolSample, ...]) -> None:
        nonlocal flaky_batches
        flaky_batches += 1
        raise RuntimeError("listener bug")

    try:
        good_handle = await driver.listen((), good_callback)
        flaky_handle = await driver.listen((), flaky_callback)
        station_point = server._station.get_point(100)  # type: ignore[union-attr]
        assert station_point is not None
        for value in (1600.0, 1700.0, 1800.0):
            station_point.value = value
            assert station_point.transmit(c104.Cot.SPONTANEOUS)
            await asyncio.sleep(0.1)

        await asyncio.wait_for(good_done.wait(), timeout=5.0)
        # 坏监听者只处理了第一批即被失效；健康监听者三拍全收。
        assert flaky_batches == 1
        assert flaky_handle.failed is True
        assert flaky_handle.task.done()
        assert good_received == [1600.0, 1700.0, 1800.0]
        assert good_handle.failed is False
        await good_handle.close()
    finally:
        await driver.close()
        await server.stop()


async def test_many_active_reads_leave_stable_resources() -> None:
    """50 个配置点全部真实 C_RD 读回：在途表归零；锁表按配置有界、不随操作增长。"""
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

        # 第一轮后：在途 future 表归零；50 个按址锁对应 50 个配置点（有界）。
        assert not driver._read
        assert not driver._control
        assert not driver._interrogation
        assert len(driver._locks) == 50

        # 第二轮重复读：锁表不得随操作次数增长，在途表仍归零。
        for index in range(50):
            sample = await driver.read_one(f"meas{1000 + index}")
            assert sample.value == pytest.approx(float(index), abs=1e-4)
        assert not driver._read
        assert len(driver._locks) == 50
        assert not driver._station_locks
    finally:
        await driver.close()
        await server.stop()

    assert not _iec104_tasks()
