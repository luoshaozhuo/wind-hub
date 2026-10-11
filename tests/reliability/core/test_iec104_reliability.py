"""Core IEC104Driver 可靠性补充矩阵（真实 c104 loopback）。

建连失败重试、断线、自动重连与补召、命令超时已由
``tests/integration/protocols/iec104`` 覆盖；本文件补齐
连接拒绝的显式失败、重复断开与主动读中取消的资源语义。
"""

from __future__ import annotations

import asyncio

import pytest

from core.application.errors import ProtocolError
from core.domain import (
    UNIT_CATALOG,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    Protocol,
    UnitCode,
)
from core.infrastructure.protocol.iec104 import IEC104Driver
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.process import free_port

pytestmark = pytest.mark.real_service


def _driver(port: int) -> IEC104Driver:
    point = Point(
        point_id="meas",
        business_point_id="meas",
        source_unit=UNIT_CATALOG[UnitCode.NONE],
        access=PointAccess.READ,
        ext={"ioa": 100, "type_id": "M_ME_NC_1"},
    )
    return IEC104Driver(
        ConnectionEndpoint("127.0.0.1", port),
        PointTable("pt", Protocol("iec104"), {"meas": point}),
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


async def test_cancel_during_active_read_leaves_no_pending_future() -> None:
    """主动 C_RD 读在途取消：不得挂起，也不得残留 per-IOA future 泄漏。"""
    server = IEC104MockServer(port=free_port())
    await server.start()
    driver = _driver(server.port)
    await driver.connect()
    try:
        # 让发送成功但响应永不到达，读停留在等待 REQUEST 的在途状态。
        async def no_response(_mapped: object) -> None:
            return None

        driver._send_read = no_response  # type: ignore[method-assign]
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
