"""应用层健康探测（真实读取证据）与点表热重载语义。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import ProtocolError
from core.domain import Point, PointTable
from core.domain import Protocol as DeviceProtocol
from core.domain.identities import PointTableId
from tests.support.iec104_hat_fake import (
    connected_driver,
    floating_data_msg,
    make_point,
    read_negative_msg,
)


def _table(points: list[Point]) -> PointTable:
    return PointTable(
        point_table_id=PointTableId("iec_pt"),
        protocol=DeviceProtocol("iec104"),
        points={point.point_id: point for point in points},
    )


async def test_health_point_positive_read_is_application_healthy() -> None:
    driver, conn = await connected_driver(
        [make_point("hp", 500)], health_point="hp", options={"t1": 2.0}
    )
    task = asyncio.create_task(driver.health())
    await asyncio.sleep(0.05)
    conn.deliver(
        [floating_data_msg(1, 500, 1.0, cause=iec101.DataResCause.REQUEST)]
    )
    health = await asyncio.wait_for(task, timeout=2)
    assert health.healthy
    assert "probe ok" in (health.message or "")
    await driver.close()


async def test_health_point_unknown_io_address_treated_as_unsupported_probe() -> None:
    driver, conn = await connected_driver(
        [make_point("hp", 500)], health_point="hp", options={"t1": 2.0}
    )
    task = asyncio.create_task(driver.health())
    await asyncio.sleep(0.05)
    conn.deliver(
        [read_negative_msg(1, 500, iec101.ReadResCause.UNKNOWN_IO_ADDRESS)]
    )
    health = await asyncio.wait_for(task, timeout=2)
    assert health.healthy
    assert "does not support active read probe" in (health.message or "")
    await driver.close()


async def test_health_point_timeout_is_application_unhealthy() -> None:
    driver, _conn = await connected_driver(
        [make_point("hp", 500)], health_point="hp", options={"t1": 0.2}
    )
    health = await driver.health()
    assert not health.healthy
    assert "application probe failed" in (health.message or "")
    await driver.close()


async def test_point_table_update_rejects_pending_but_keeps_session_usable() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)], options={"t1": 2.0})
    pending = asyncio.create_task(driver.read_one("p1"))
    await asyncio.sleep(0.05)

    driver.update_point_table(_table([make_point("p1", 100), make_point("p2", 200)]))
    with pytest.raises(ProtocolError, match="point table changed"):
        await pending
    assert driver.is_open()  # 会话不断开

    # 新表立即生效：读取 p2 能正常关联。
    read2 = asyncio.create_task(driver.read_one("p2"))
    await asyncio.sleep(0.05)
    conn.deliver(
        [floating_data_msg(1, 200, 3.5, cause=iec101.DataResCause.REQUEST)]
    )
    sample = await asyncio.wait_for(read2, timeout=2)
    assert sample.point_id == "p2"
    await driver.close()


async def test_peer_disabling_data_transfer_fails_pending_and_closes_health() -> None:
    driver, conn = await connected_driver([make_point("p1", 100)])
    pending = asyncio.create_task(driver.read_one("p1"))
    await asyncio.sleep(0.05)
    conn.notify_enabled(False)
    with pytest.raises(ProtocolError):
        await pending
    assert driver.is_open() is False
    health = await driver.health()
    assert not health.healthy and "STARTDT" in (health.message or "")
    await driver.close()
