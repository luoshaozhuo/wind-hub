"""超时/否定确认后的会话隔离：在途操作必须收敛且旧会话不可再用。

任何不确定响应（t1 超时、读取否定、站召否定）都要求 driver 主动隔离当前
session：在途 future 以异常或 UNKNOWN 收敛、is_open() 变 False、生命周期
进入 disconnected（物理关闭失败才 faulted）。
"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers import iec104
from hat.drivers.iec101 import common as iec101

from core.application.errors import ProtocolError
from core.application.protocol_contract import (
    InterrogationRequest,
    ProtocolLifecycleState,
    ProtocolWrite,
    WriteOutcome,
)
from core.domain import PointAccess
from core.infrastructure.protocol.iec104.driver import (
    IEC104InterrogationIncompleteError,
)
from tests.support.iec104_fake import (
    connect_driver,
    interrogation_reply,
    make_driver,
    make_point,
)

pytestmark = pytest.mark.asyncio

FAST_TIMEOUTS = {"t1": 0.2, "t2": 0.1, "t3": 30.0, "t0": 2.0}


async def _assert_isolated(driver: object) -> None:
    # _isolate 内 await session.close() 完成后状态才落定，给事件循环一拍。
    for _ in range(20):
        if not driver.is_open() and driver._closing is None:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0)
    assert driver.is_open() is False
    assert driver.runtime_state().lifecycle is ProtocolLifecycleState.DISCONNECTED


async def test_read_timeout_closes_session_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = make_point("wind", 100)
    driver = make_driver([point], options=FAST_TIMEOUTS)
    conn = await connect_driver(driver, monkeypatch)

    with pytest.raises(TimeoutError):
        await driver.read_one("wind")

    await _assert_isolated(driver)
    assert conn.closed is True

    # 隔离后的会话不能再发请求。
    with pytest.raises(Exception, match="unavailable"):
        await driver.read_one("wind")
    await driver.close()


async def test_negative_read_rejection_isolates_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = make_point("wind", 999)
    driver = make_driver([point], options=FAST_TIMEOUTS)
    conn = await connect_driver(driver, monkeypatch)

    task = asyncio.create_task(driver.read_one("wind"))
    await asyncio.sleep(0.05)
    # 对端以否定读响应（UNKNOWN_IO_ADDRESS）回复。
    conn.deliver(
        [
            iec104.ReadMsg(
                is_test=False,
                originator_address=0,
                asdu_address=1,
                io_address=999,
                cause=iec101.ReadResCause.UNKNOWN_IO_ADDRESS,
            )
        ]
    )

    with pytest.raises(ProtocolError, match="read rejected"):
        await asyncio.wait_for(task, timeout=1)
    await _assert_isolated(driver)
    await driver.close()


async def test_write_timeout_returns_unknown_and_isolates_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = make_point(
        "setpoint",
        200,
        type_id="C_SE_NC_1",
        access=PointAccess.WRITE,
    )
    driver = make_driver([point], options=FAST_TIMEOUTS)
    conn = await connect_driver(driver, monkeypatch)

    result = await driver.write_one(ProtocolWrite("setpoint", 42.0))

    assert result.outcome is WriteOutcome.UNKNOWN
    await _assert_isolated(driver)
    assert conn.closed is True
    await driver.close()


async def test_negative_interrogation_confirmation_isolates_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = make_point("wind", 100)
    driver = make_driver([point], options=FAST_TIMEOUTS)
    conn = await connect_driver(driver, monkeypatch)

    task = asyncio.create_task(driver.interrogate(InterrogationRequest(kind="station")))
    await asyncio.sleep(0.05)
    assert isinstance(conn.last_message(), iec104.InterrogationMsg)
    # ACT_CON 否定确认：站召在激活阶段即被拒绝。
    conn.deliver([interrogation_reply(1, negative=True)])

    with pytest.raises(IEC104InterrogationIncompleteError):
        await asyncio.wait_for(task, timeout=1)
    await _assert_isolated(driver)
    await driver.close()
