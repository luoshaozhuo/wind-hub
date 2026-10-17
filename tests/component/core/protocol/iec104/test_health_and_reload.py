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
    assert "application probe unsupported" in (health.message or "")
    # 显式 C_RD 拒绝不断开连接、不触发恢复动作。
    assert driver.is_open()
    await driver.close()


async def test_health_point_timeout_is_application_unhealthy() -> None:
    driver, _conn = await connected_driver(
        [make_point("hp", 500)], health_point="hp", options={"t1": 0.2}
    )
    health = await driver.health()
    assert not health.healthy
    assert "application probe timed out" in (health.message or "")
    await driver.close()


async def test_health_without_health_point_reports_link_only_evidence() -> None:
    """未配置 health_point：只报告链路/APCI 证据，文案显式声明无应用探测。"""
    driver, conn = await connected_driver([make_point("p1", 100)])
    health = await driver.health()
    assert health.healthy
    message = health.message or ""
    assert "link/APCI evidence only" in message
    assert "no application probe" in message
    # 没有发出任何 C_RD。
    assert conn.sent_messages == []
    await driver.close()


async def test_health_bad_quality_sample_still_means_communication_healthy() -> None:
    """坏品质是合法通信证据：probe 拿到新鲜响应即健康，不按点值有效性判健康。"""
    driver, conn = await connected_driver(
        [make_point("hp", 500)], health_point="hp", options={"t1": 2.0}
    )
    task = asyncio.create_task(driver.health())
    await asyncio.sleep(0.05)
    conn.deliver(
        [
            floating_data_msg(
                1, 500, 1.0, invalid=True, cause=iec101.DataResCause.REQUEST
            )
        ]
    )
    health = await asyncio.wait_for(task, timeout=2)
    assert health.healthy
    assert "application probe ok" in (health.message or "")
    await driver.close()


async def test_health_malformed_probe_response_is_application_unhealthy() -> None:
    """探测响应数据类型不受支持：显式读取失败（ProtocolError），健康转差。"""
    from tests.component.core.protocol.iec104.test_receive_resilience import (
        _inject,
        _unsupported_data_frame,
    )

    driver, conn = await connected_driver(
        [make_point("hp", 500)], health_point="hp", options={"t1": 2.0}
    )
    task = asyncio.create_task(driver.health())
    await asyncio.sleep(0.05)
    _inject(conn, _unsupported_data_frame(500, cause_byte=5))
    health = await asyncio.wait_for(task, timeout=2)
    assert not health.healthy
    assert "application probe failed" in (health.message or "")
    assert driver.is_open()  # 单次探测失败不触发断开
    await driver.close()


async def test_health_when_not_connected_is_unhealthy() -> None:
    driver, _conn = await connected_driver([make_point("hp", 500)], health_point="hp")
    await driver.close()
    health = await driver.health()
    assert not health.healthy
    assert "not connected" in (health.message or "")


async def test_health_probe_unsupported_message_names_multi_ca_address() -> None:
    """多 CA 场景：探测不支持的诊断消息带完整 ca/ioa 地址。"""
    driver, conn = await connected_driver(
        [make_point("hp", 500, ca=7)],
        health_point="hp",
        common_addresses="1,7",
        options={"t1": 2.0},
    )
    task = asyncio.create_task(driver.health())
    await asyncio.sleep(0.05)
    conn.deliver(
        [read_negative_msg(7, 500, iec101.ReadResCause.UNKNOWN_IO_ADDRESS)]
    )
    health = await asyncio.wait_for(task, timeout=2)
    assert health.healthy
    assert "ca=7" in (health.message or "")
    assert "ioa=500" in (health.message or "")
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
