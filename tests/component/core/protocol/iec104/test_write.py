"""遥控/设点写入的三分类：SUCCESS / CONFIRMED_FAILURE / UNKNOWN。"""

from __future__ import annotations

import asyncio

from hat.drivers.iec101 import common as iec101

from core.application.protocol_contract import ProtocolWrite, WriteOutcome
from core.domain import PointAccess
from tests.support.iec104_hat_fake import (
    command_confirmation_msg,
    connected_driver,
    make_point,
)


def _cmd(point_id: str = "breaker", ioa: int = 1001) -> ProtocolWrite:
    return ProtocolWrite(point=point_id, value=True)


async def test_positive_act_con_and_term_confirm_success() -> None:
    points = [make_point("breaker", 1001, type_id="C_SC_NA_1", access=PointAccess.WRITE)]
    driver, conn = await connected_driver(points)
    task = asyncio.create_task(driver.write_one(_cmd()))
    await asyncio.sleep(0.05)
    conn.deliver(
        [command_confirmation_msg(1, 1001, cause=iec101.CommandResCause.ACTIVATION_CONFIRMATION)]
    )
    assert not task.done()
    conn.deliver(
        [command_confirmation_msg(1, 1001, cause=iec101.CommandResCause.ACTIVATION_TERMINATION)]
    )
    result = await asyncio.wait_for(task, timeout=1)
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    await driver.close()


async def test_negative_act_con_is_confirmed_failure() -> None:
    points = [make_point("breaker", 1001, type_id="C_SC_NA_1", access=PointAccess.WRITE)]
    driver, conn = await connected_driver(points)
    task = asyncio.create_task(driver.write_one(_cmd()))
    await asyncio.sleep(0.05)
    conn.deliver([command_confirmation_msg(1, 1001, negative=True)])
    result = await asyncio.wait_for(task, timeout=1)
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert result.message
    await driver.close()


async def test_unknown_io_confirmation_is_confirmed_failure() -> None:
    points = [make_point("breaker", 1001, type_id="C_SC_NA_1", access=PointAccess.WRITE)]
    driver, conn = await connected_driver(points)
    task = asyncio.create_task(driver.write_one(_cmd()))
    await asyncio.sleep(0.05)
    conn.deliver(
        [
            command_confirmation_msg(
                1,
                1001,
                cause=iec101.CommandResCause.UNKNOWN_IO_ADDRESS,
                negative=True,
            )
        ]
    )
    result = await asyncio.wait_for(task, timeout=1)
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    await driver.close()


async def test_confirmation_timeout_is_unknown() -> None:
    points = [make_point("breaker", 1001, type_id="C_SC_NA_1", access=PointAccess.WRITE)]
    driver, _conn = await connected_driver(points, options={"t1": 0.2})
    result = await driver.write_one(_cmd())
    assert result.outcome is WriteOutcome.UNKNOWN
    assert "may have executed" in (result.message or "")
    await driver.close()


async def test_local_type_validation_failure_is_confirmed_failure() -> None:
    points = [make_point("breaker", 1001, type_id="C_SC_NA_1", access=PointAccess.WRITE)]
    driver, conn = await connected_driver(points)
    result = await driver.write_one(ProtocolWrite(point="breaker", value="ON"))
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert result.message
    assert conn.sent == []  # 本地校验失败绝不发帧
    await driver.close()


async def test_connection_close_after_send_is_unknown_not_failure() -> None:
    points = [make_point("breaker", 1001, type_id="C_SC_NA_1", access=PointAccess.WRITE)]
    driver, conn = await connected_driver(points)
    task = asyncio.create_task(driver.write_one(_cmd()))
    await asyncio.sleep(0.05)
    await conn.async_close()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.outcome is WriteOutcome.UNKNOWN
    await driver.close()
