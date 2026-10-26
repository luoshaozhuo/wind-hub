"""C_RD_NA_1 主动读取：真实 ReadMsg、(ca,ioa) 关联、类型校验、超时/断线/取消。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101
from hat.drivers.iec104 import common as iec104

from core.application.errors import ConfigError, ProtocolError
from tests.support.iec104_fake import (
    FakeConnection,
    floating_data_msg,
    make_driver,
    make_point,
    make_table,
)

pytestmark = pytest.mark.asyncio


def wire(monkeypatch, conn):
    async def _connect(addr, **kwargs):
        del addr, kwargs
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _connect)


async def wait_until(predicate, timeout=1.0):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def single_msg(ca, ioa, value=True, *, cause=iec101.DataResCause.REQUEST):
    return iec101.DataMsg(
        is_test=False,
        originator_address=0,
        asdu_address=ca,
        io_address=ioa,
        data=iec101.SingleData(
            iec101.SingleValue.ON if value else iec101.SingleValue.OFF,
            iec101.MeasurementQuality(
                invalid=False, not_topical=False, substituted=False, blocked=False, overflow=False
            ),
        ),
        time=None,
        cause=cause,
    )


async def test_active_read_sends_c_rd_with_correct_address(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)])
    await driver.connect()
    task = asyncio.create_task(driver.read_one("wind"))
    await wait_until(lambda: bool(conn.sent_messages))
    sent = conn.last_message()
    assert isinstance(sent, iec104.ReadMsg)
    assert sent.asdu_address == 1 and sent.io_address == 100
    assert sent.cause == iec104.ReadReqCause.REQUEST

    conn.deliver([floating_data_msg(1, 100, 5.0, cause=iec101.DataResCause.REQUEST)])
    sample = await asyncio.wait_for(task, 1)
    assert sample.value == 5.0
    await driver.close()


async def test_response_for_other_address_does_not_complete(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver(
        [
            make_point("a", 100, ca=1),
            make_point("b", 100, ca=2),
        ],
        common_addresses="1,2",
    )
    await driver.connect()

    task = asyncio.create_task(driver.read_one("a"))
    await wait_until(lambda: bool(conn.sent_messages))
    # 同 IOA 但不同 CA 的响应不得完成 a。
    conn.deliver([floating_data_msg(2, 100, 1.0, cause=iec101.DataResCause.REQUEST)])
    await asyncio.sleep(0.03)
    assert not task.done()
    conn.deliver([floating_data_msg(1, 100, 2.0, cause=iec101.DataResCause.REQUEST)])
    assert (await asyncio.wait_for(task, 1)).value == 2.0
    await driver.close()


async def test_type_id_mismatch_fails_and_isolates(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("sw", 100, type_id="M_SP_NA_1")])
    await driver.connect()
    task = asyncio.create_task(driver.read_one("sw"))
    await wait_until(lambda: bool(conn.sent_messages))
    # 点配置单点 M_SP，却回浮点 M_ME_NC → 类型不一致，确定失败并隔离。
    conn.deliver([floating_data_msg(1, 100, 1.0, cause=iec101.DataResCause.REQUEST)])
    with pytest.raises(ProtocolError):
        await task
    assert not driver.is_open()


async def test_read_act_con_echo_ignored_and_data_completes(monkeypatch):
    """真实从站先回 ReadMsg(cause=7 ACT_CON 回显) 再回 DataMsg(cause=5)。

    回显帧必须被忽略，由后续 DataMsg 完成读取（c104/lib60870 标准行为）。
    """
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)])
    await driver.connect()
    task = asyncio.create_task(driver.read_one("wind"))
    await wait_until(lambda: bool(conn.sent_messages))
    echo = iec104.ReadMsg(
        is_test=False,
        originator_address=0,
        asdu_address=1,
        io_address=100,
        cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION,
    )
    conn.deliver([echo])
    await asyncio.sleep(0.03)
    assert not task.done()  # 回显不得判死读取
    conn.deliver([floating_data_msg(1, 100, 5.0, cause=iec101.DataResCause.REQUEST)])
    assert (await asyncio.wait_for(task, 1)).value == 5.0
    assert driver.is_open()  # 回显帧不得隔离会话
    await driver.close()


async def test_negative_read_response_fails(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)])
    await driver.connect()
    task = asyncio.create_task(driver.read_one("wind"))
    await wait_until(lambda: bool(conn.sent_messages))
    # 否定读：cause=47 UNKNOWN_IO_ADDRESS（仅 44–47 才是否定确认）。
    negative = iec104.ReadMsg(
        is_test=False,
        originator_address=0,
        asdu_address=1,
        io_address=100,
        cause=iec101.ReadResCause.UNKNOWN_IO_ADDRESS,
    )
    conn.deliver([negative])
    with pytest.raises(ProtocolError):
        await task
    assert not driver.is_open()
    await driver.close()


async def test_read_timeout_isolates_session(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)], options={"t1": 0.15, "t2": 0.05})
    await driver.connect()
    with pytest.raises(TimeoutError):
        await driver.read_one("wind")
    assert not driver.is_open()


async def test_read_cancelled_propagates(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)], options={"t1": 5.0})
    await driver.connect()
    task = asyncio.create_task(driver.read_one("wind"))
    await wait_until(lambda: bool(conn.sent_messages))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await driver.close()


async def test_concurrent_same_address_reads_serialized(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)])
    await driver.connect()

    values = iter([1.0, 2.0])

    async def responder():
        for value in values:
            await wait_until(
                lambda: sum(isinstance(m, iec104.ReadMsg) for m in conn.sent_messages)
                > responder.sent
            )
            responder.sent = len([m for m in conn.sent_messages if isinstance(m, iec104.ReadMsg)])
            conn.deliver([floating_data_msg(1, 100, value, cause=iec101.DataResCause.REQUEST)])
            await asyncio.sleep(0.01)

    responder.sent = 0

    results = await asyncio.gather(driver.read_one("wind"), driver.read_one("wind"), responder())
    assert results[0].value == 1.0 and results[1].value == 2.0
    read_msgs = [m for m in conn.sent_messages if isinstance(m, iec104.ReadMsg)]
    assert len(read_msgs) == 2
    await driver.close()


async def test_read_requires_monitoring_type(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    p = make_point(
        "cmd",
        200,
        type_id="C_SC_NA_1",
        access=__import__("core").domain.PointAccess.WRITE,
    )
    driver = make_driver([p])
    await driver.connect()
    with pytest.raises(ConfigError):
        await driver.read_one("cmd")
    await driver.close()


async def test_hot_update_removing_point_fails_later_read(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)])
    await driver.connect()
    # 热更新点表后，已删除点不再可主动读取；在途请求使用提交时快照。
    driver.update_point_table(make_table([]))
    with pytest.raises(ConfigError):
        await driver.read_one("wind")
    await driver.close()
