"""总召关联：逐 CA 等待 ACT_TERM，否定/超时显式失败。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import ProtocolError
from tests.support.iec104_hat_fake import (
    connected_driver,
    interrogation_msg,
    make_point,
)


async def test_interrogation_waits_for_act_term_not_just_act_con() -> None:
    driver, conn = await connected_driver([make_point("p", 100)])
    task = asyncio.create_task(driver.interrogate())
    await asyncio.sleep(0.05)
    conn.deliver([interrogation_msg(1, cause=iec101.CommandResCause.ACTIVATION_CONFIRMATION)])
    await asyncio.sleep(0.05)
    assert not task.done()  # ACT_CON 不是总召完成

    conn.deliver([interrogation_msg(1, cause=iec101.CommandResCause.ACTIVATION_TERMINATION)])
    await asyncio.wait_for(task, timeout=1)
    sent = conn.last_message()
    assert isinstance(sent, iec101.InterrogationMsg)
    assert sent.request == 20
    await driver.close()


async def test_negative_act_con_fails_interrogation() -> None:
    driver, conn = await connected_driver([make_point("p", 100)])
    task = asyncio.create_task(driver.interrogate())
    await asyncio.sleep(0.05)
    conn.deliver(
        [interrogation_msg(1, cause=iec101.CommandResCause.ACTIVATION_CONFIRMATION, negative=True)]
    )
    with pytest.raises(ProtocolError, match="rejected"):
        await asyncio.wait_for(task, timeout=1)
    await driver.close()


async def test_interrogation_timeout_without_act_term() -> None:
    driver, _conn = await connected_driver([make_point("p", 100)], options={"t1": 0.2})
    with pytest.raises(ProtocolError, match="ACT_TERM timed out"):
        await driver.interrogate()
    await driver.close()


async def test_default_interrogation_iterates_all_configured_cas_in_order() -> None:
    points = [make_point("ca1", 100, ca=1), make_point("ca2", 100, ca=2)]
    driver, conn = await connected_driver(points, common_addresses="1,2", options={"t1": 2.0})
    task = asyncio.create_task(driver.interrogate())
    await asyncio.sleep(0.05)

    # 先完成 CA1（ACT_CON + ACT_TERM），CA2 随后完成。
    conn.deliver([interrogation_msg(1, cause=iec101.CommandResCause.ACTIVATION_CONFIRMATION)])
    conn.deliver([interrogation_msg(1, cause=iec101.CommandResCause.ACTIVATION_TERMINATION)])
    await asyncio.sleep(0.05)
    assert not task.done()
    conn.deliver([interrogation_msg(2, cause=iec101.CommandResCause.ACTIVATION_CONFIRMATION)])
    conn.deliver([interrogation_msg(2, cause=iec101.CommandResCause.ACTIVATION_TERMINATION)])
    await asyncio.wait_for(task, timeout=2)

    interrogations = [m for m in conn.sent_messages if isinstance(m, iec101.InterrogationMsg)]
    assert [m.asdu_address for m in interrogations] == [1, 2]
    await driver.close()
