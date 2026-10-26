"""唯一 receive() 循环：所有入站报文只经一个接收任务，混合批次正确路由。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101
from hat.drivers.iec104 import common as iec104

from core.application.errors import ProtocolConnectionError
from core.application.protocol_contract import ProtocolWrite
from core.domain import PointAccess
from tests.support.iec104_fake import (
    FakeConnection,
    command_msg,
    floating_data_msg,
    make_driver,
    make_point,
)

pytestmark = pytest.mark.asyncio


class GuardedConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.active_receives = 0
        self.max_concurrent = 0

    async def receive(self):
        self.active_receives += 1
        self.max_concurrent = max(self.max_concurrent, self.active_receives)
        try:
            return await super().receive()
        finally:
            self.active_receives -= 1


def wire(monkeypatch, conn):
    async def _connect(addr, **kwargs):
        del addr, kwargs
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _connect)


def points():
    return [
        make_point("wind", 100, type_id="M_ME_NC_1"),
        make_point("cmd", 200, type_id="C_SC_NA_1", access=PointAccess.WRITE),
    ]


async def test_single_receiver_no_parallel_receive(monkeypatch):
    conn = GuardedConnection()
    wire(monkeypatch, conn)
    driver = make_driver(points())
    await driver.connect()
    for _ in range(5):
        conn.deliver([floating_data_msg(1, 100, 1.0)])
        await asyncio.sleep(0.01)
    assert conn.max_concurrent == 1
    await driver.close()


async def test_mixed_batch_routes_data_read_and_command(monkeypatch):
    conn = GuardedConnection()
    wire(monkeypatch, conn)
    driver = make_driver(points())
    await driver.connect()

    spontaneous = []
    await driver.listen(["wind"], spontaneous.append)

    write = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    read = asyncio.create_task(driver.read_one("wind"))
    await asyncio.sleep(0.02)

    sent_command = [m for m in conn.sent_messages if isinstance(m, iec104.CommandMsg)][-1]
    conn.deliver(
        [
            floating_data_msg(1, 100, 9.5),
            floating_data_msg(1, 100, 4.25, cause=iec101.DataResCause.REQUEST),
            command_msg(
                1, 200, sent_command.command, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            ),
            command_msg(
                1, 200, sent_command.command, cause=iec104.CommandResCause.ACTIVATION_TERMINATION
            ),
        ]
    )
    results = await asyncio.wait_for(asyncio.gather(write, read), timeout=2)

    assert any(batch[0].value == 9.5 for batch in spontaneous)
    assert results[1].value == 4.25
    assert results[0].outcome.value == "confirmed_success"
    await driver.close()


async def test_unknown_ioa_dropped_but_loop_survives(monkeypatch):
    conn = GuardedConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)])
    await driver.connect()
    got = []
    await driver.listen(["wind"], got.append)

    # 点表中不存在的 (ca,ioa)：丢弃且接收循环继续工作。
    conn.deliver([floating_data_msg(1, 999, 1.0)])
    await asyncio.sleep(0.02)
    conn.deliver([floating_data_msg(1, 100, 2.0)])
    await asyncio.sleep(0.02)
    assert any(batch[0].value == 2.0 for batch in got)
    await driver.close()


async def test_receive_exception_converges_inflight_operations(monkeypatch):
    conn = GuardedConnection()
    wire(monkeypatch, conn)
    driver = make_driver(points(), options={"t1": 5.0})
    await driver.connect()

    read = asyncio.create_task(driver.read_one("wind"))
    write = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await asyncio.sleep(0.02)
    # 对端 STOPDT/异常断开。
    conn.notify_enabled(False)

    with pytest.raises(ProtocolConnectionError):
        await read
    # 写在隔离路径上以 UNKNOWN 收敛（命令可能已发出），不产生未捕获异常。
    result = await write
    assert result.outcome.value == "unknown"
    await driver.close()


async def test_late_request_data_without_pending_read_is_delivered(monkeypatch):
    conn = GuardedConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("wind", 100)])
    await driver.connect()
    got = []
    await driver.listen(["wind"], got.append)

    # 无在途主动读取时到达的 REQUEST 数据：作为普通样本投递，不崩溃。
    conn.deliver([floating_data_msg(1, 100, 3.0, cause=iec101.DataResCause.REQUEST)])
    await asyncio.sleep(0.02)
    assert any(batch[0].value == 3.0 for batch in got)
    await driver.close()
