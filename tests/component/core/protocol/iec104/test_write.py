"""遥控/设点：direct/SBO、act_con/act_term、否定、超时、去重与地址关联。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101
from hat.drivers.iec104 import common as iec104

from core.application.protocol_contract import ProtocolWrite, WriteOutcome
from core.domain import PointAccess
from tests.support.iec104_fake import (
    FakeConnection,
    command_msg,
    make_driver,
    make_point,
)

pytestmark = pytest.mark.asyncio


def wire(monkeypatch, conn):
    async def _connect(addr, **kwargs):
        del addr, kwargs
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _connect)


def cmd_point(point_id="cmd", ioa=200, ca=1, *, mode="direct", conf="act_term"):
    return make_point(
        point_id,
        ioa,
        ca=ca,
        type_id="C_SC_NA_1",
        access=PointAccess.WRITE,
        extra={"command_mode": mode, "confirmation": conf},
    )


async def pump_until(predicate, timeout=1.0):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def commands(conn):
    return [m for m in conn.sent_messages if isinstance(m, iec104.CommandMsg)]


def confirm_for(sent, *, cause, negative=False):
    return command_msg(
        sent.asdu_address, sent.io_address, sent.command, cause=cause, negative=negative
    )


async def test_direct_act_term_success(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point()])
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: commands(conn))
    sent = commands(conn)[-1]
    conn.deliver(
        [
            confirm_for(sent, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION),
            confirm_for(sent, cause=iec104.CommandResCause.ACTIVATION_TERMINATION),
        ]
    )
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    await driver.close()


async def test_act_con_success_returns_on_confirmation(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point(conf="act_con")])
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: commands(conn))
    sent = commands(conn)[-1]
    conn.deliver([confirm_for(sent, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION)])
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    # ACT_TERM 未到：重复控制必须被拒绝（unsettled），且不再发命令。
    sent_before = len(commands(conn))
    duplicate = await driver.write_one(ProtocolWrite(point="cmd", value=True))
    assert duplicate.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert len(commands(conn)) == sent_before
    await driver.close()


async def test_negative_act_con_is_confirmed_failure(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point()])
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: commands(conn))
    sent = commands(conn)[-1]
    conn.deliver(
        [confirm_for(sent, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION, negative=True)]
    )
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    await driver.close()


async def test_unknown_cause_is_confirmed_failure(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point()])
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: commands(conn))
    sent = commands(conn)[-1]
    conn.deliver([confirm_for(sent, cause=iec104.CommandResCause.UNKNOWN_IO_ADDRESS)])
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    await driver.close()


async def test_timeout_is_unknown_no_automatic_resend(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point(conf="act_con")], options={"t1": 0.2, "t2": 0.05})
    await driver.connect()
    result = await driver.write_one(ProtocolWrite(point="cmd", value=True))
    assert result.outcome is WriteOutcome.UNKNOWN
    # 驱动层不得因超时自动重发：只发出一条命令，且会话被隔离。
    assert len(commands(conn)) == 1
    assert not driver.is_open()


async def test_sbo_select_then_execute(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point(mode="select_execute", conf="act_con")])
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: len(commands(conn)) == 1)
    select_sent = commands(conn)[0]
    assert select_sent.command.select is True
    conn.deliver([confirm_for(select_sent, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION)])
    await pump_until(lambda: len(commands(conn)) == 2)
    execute_sent = commands(conn)[1]
    assert execute_sent.command.select is False
    conn.deliver([confirm_for(execute_sent, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION)])
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    await driver.close()


async def test_sbo_select_rejected_does_not_execute(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point(mode="select_execute", conf="act_con")])
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: commands(conn))
    select_sent = commands(conn)[0]
    conn.deliver(
        [
            confirm_for(
                select_sent, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION, negative=True
            )
        ]
    )
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert len(commands(conn)) == 1  # 未发 execute
    await driver.close()


async def test_sbo_execute_rejected_sends_deactivation(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point(mode="select_execute", conf="act_con")])
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: commands(conn))
    sel = commands(conn)[0]
    conn.deliver([confirm_for(sel, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION)])
    await pump_until(lambda: len(commands(conn)) == 2)
    exe = commands(conn)[1]
    # execute 否定 → 触发 DEACT。
    conn.deliver(
        [confirm_for(exe, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION, negative=True)]
    )
    await pump_until(lambda: len(commands(conn)) == 3)
    deact = commands(conn)[2]
    assert deact.cause == iec104.CommandReqCause.DEACTIVATION
    conn.deliver([confirm_for(deact, cause=iec104.CommandResCause.DEACTIVATION_CONFIRMATION)])
    result = await asyncio.wait_for(task, 1)
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    await driver.close()


async def test_local_value_validation_failure_is_confirmed_failure(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point()])
    await driver.connect()
    # C_SC_NA_1 需要 bool。
    result = await driver.write_one(ProtocolWrite(point="cmd", value=123))
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert not commands(conn)  # 请求未发送
    await driver.close()


async def test_confirmation_for_other_value_not_associated(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([cmd_point(conf="act_con")], options={"t1": 0.3, "t2": 0.05})
    await driver.connect()
    task = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await pump_until(lambda: commands(conn))
    sent = commands(conn)[-1]
    # 回送命令值不匹配（OFF 而非 ON）→ 不关联，等待超时 → UNKNOWN。
    other = iec101.SingleCommand(value=iec101.SingleValue.OFF, select=False, qualifier=0)
    conn.deliver([command_msg(1, 200, other, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION)])
    result = await asyncio.wait_for(task, 2)
    assert result.outcome is WriteOutcome.UNKNOWN
    assert sent is not None


async def test_same_ioa_different_ca_routed_independently(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver(
        [cmd_point("c1", 200, ca=1, conf="act_con"), cmd_point("c2", 200, ca=2, conf="act_con")],
        common_addresses="1,2",
    )
    await driver.connect()
    t1 = asyncio.create_task(driver.write_one(ProtocolWrite(point="c1", value=True)))
    await pump_until(lambda: any(m.asdu_address == 1 for m in commands(conn)))
    sent_ca1 = next(m for m in commands(conn) if m.asdu_address == 1)
    # CA2 的确认绝不能完成 CA1。
    conn.deliver(
        [
            command_msg(
                2, 200, sent_ca1.command, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            )
        ]
    )
    await asyncio.sleep(0.03)
    assert not t1.done()
    conn.deliver(
        [
            command_msg(
                1, 200, sent_ca1.command, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            )
        ]
    )
    assert (await asyncio.wait_for(t1, 1)).outcome is WriteOutcome.CONFIRMED_SUCCESS
    await driver.close()
