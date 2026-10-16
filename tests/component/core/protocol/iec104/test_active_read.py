"""C_RD_NA_1 主动读取的请求-响应关联（完整 (ca,ioa) 地址 + COT）。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import ProtocolConnectionError
from core.infrastructure.protocol.iec104.driver import (
    IEC104ActiveReadRejectedError,
)
from tests.support.iec104_hat_fake import (
    connected_driver,
    floating_data_msg,
    make_point,
    read_negative_msg,
)


async def test_read_one_returns_fresh_request_cause_response() -> None:
    driver, conn = await connected_driver([make_point("p", 100)])
    task = asyncio.create_task(driver.read_one("p"))
    await asyncio.sleep(0.05)

    conn.deliver(
        [floating_data_msg(1, 100, 7.5, cause=iec101.DataResCause.REQUEST)]
    )
    sample = await asyncio.wait_for(task, timeout=1)
    assert sample.point_id == "p"
    assert sample.value == pytest.approx(7.5)
    sent = conn.last_message()
    assert isinstance(sent, iec101.ReadMsg)
    assert sent.cause is iec101.ReadReqCause.REQUEST
    await driver.close()


async def test_spontaneous_update_does_not_complete_pending_read() -> None:
    driver, conn = await connected_driver([make_point("p", 100)])
    task = asyncio.create_task(driver.read_one("p"))
    await asyncio.sleep(0.05)

    conn.deliver(
        [floating_data_msg(1, 100, 9.9, cause=iec101.DataResCause.SPONTANEOUS)]
    )
    # 真正的请求响应随后到达。
    await asyncio.sleep(0.05)
    assert not task.done()
    conn.deliver(
        [floating_data_msg(1, 100, 7.5, cause=iec101.DataResCause.REQUEST)]
    )
    sample = await asyncio.wait_for(task, timeout=1)
    assert sample.value == pytest.approx(7.5)
    await driver.close()


async def test_negative_read_response_raises_application_rejection() -> None:
    driver, conn = await connected_driver([make_point("p", 999)])
    task = asyncio.create_task(driver.read_one("p"))
    await asyncio.sleep(0.05)
    conn.deliver(
        [read_negative_msg(1, 999, iec101.ReadResCause.UNKNOWN_IO_ADDRESS)]
    )
    with pytest.raises(IEC104ActiveReadRejectedError):
        await task
    await driver.close()


async def test_read_times_out_without_response() -> None:
    driver, _conn = await connected_driver([make_point("p", 100)])
    with pytest.raises(Exception, match="timed out"):
        await driver.read_one("p")
    await driver.close()


async def test_close_fails_pending_read_with_connection_error() -> None:
    driver, _conn = await connected_driver([make_point("p", 100)])
    task = asyncio.create_task(driver.read_one("p"))
    await asyncio.sleep(0.05)
    await driver.close()
    with pytest.raises(ProtocolConnectionError):
        await task


async def test_read_response_for_other_ca_does_not_correlate() -> None:
    points = [make_point("ca1", 100, ca=1), make_point("ca2", 100, ca=2)]
    driver, conn = await connected_driver(points, common_addresses="1,2")
    task = asyncio.create_task(driver.read_one("ca1"))
    await asyncio.sleep(0.05)
    # 同 IOA、错误 CA 的响应不得完成 ca1 的读取。
    conn.deliver(
        [floating_data_msg(2, 100, 5.0, cause=iec101.DataResCause.REQUEST)]
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    conn.deliver(
        [floating_data_msg(1, 100, 8.0, cause=iec101.DataResCause.REQUEST)]
    )
    sample = await asyncio.wait_for(task, timeout=1)
    assert sample.value == pytest.approx(8.0)
    await driver.close()
