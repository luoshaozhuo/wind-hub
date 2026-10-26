"""健康检查、能力声明与点表热更新。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import (
    ConfigError,
    ProtocolCapabilityError,
    ProtocolConnectionError,
)
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
)
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


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def pump_until(predicate, timeout=1.0):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


async def test_capabilities_exact_set(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 1)])
    assert driver.capabilities() == frozenset(
        {
            ProtocolCapability.READ,
            ProtocolCapability.WRITE,
            ProtocolCapability.INTERROGATE,
            ProtocolCapability.LISTEN,
        }
    )
    # 批量与远程订阅必须明确拒绝。
    with pytest.raises(ProtocolCapabilityError):
        await driver.read_many(["a"])
    with pytest.raises(ProtocolCapabilityError):
        await driver.write_many([ProtocolWrite(point="a", value=1)])
    with pytest.raises(ProtocolCapabilityError):
        driver.subscribe(["a"], lambda s: None)


async def test_health_reflects_link_and_active_read(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("hp", 1)], options={"health_point": "hp"})
    # 未连接即不健康。
    assert (await driver.health()).healthy is False
    await driver.connect()

    async def respond():
        await pump_until(lambda: bool(conn.sent_messages))
        conn.deliver([floating_data_msg(1, 1, 1.0, cause=iec101.DataResCause.REQUEST)])

    task = asyncio.create_task(respond())
    health = await driver.health()
    await task
    assert health.healthy
    await driver.close()
    assert (await driver.health()).healthy is False


async def test_health_failure_when_health_point_read_errors(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver(
        [make_point("hp", 1, type_id="M_ME_NC_1")],
        options={"health_point": "hp", "t1": 0.15, "t2": 0.05},
    )
    await driver.connect()
    health = await driver.health()  # 不回包 → 读超时隔离
    assert health.healthy is False
    assert not driver.is_open()


async def test_listen_requires_open_session(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 1)])
    with pytest.raises(ProtocolConnectionError):
        await driver.listen(["a"], lambda s: None)


async def test_hot_reload_adds_and_removes_points(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 1)])
    await driver.connect()

    new_points = [
        make_point("a", 1),
        make_point("b", 2, type_id="M_ME_NC_1"),
    ]
    driver.update_point_table(make_table(new_points))

    task = asyncio.create_task(driver.read_one("b"))
    await pump_until(lambda: bool(conn.sent_messages))
    conn.deliver([floating_data_msg(1, 2, 3.0, cause=iec101.DataResCause.REQUEST)])
    assert (await asyncio.wait_for(task, 1)).value == 3.0

    # 删除点表后该点不可读。
    driver.update_point_table(make_table([make_point("a", 1)]))
    with pytest.raises(ConfigError):
        await driver.read_one("b")
    await driver.close()


async def test_hot_reload_rejects_wrong_protocol(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 1)])
    await driver.connect()
    other = make_table([make_point("a", 1)])
    object.__setattr__(other, "protocol", __import__("core").domain.Protocol("modbus"))
    with pytest.raises(ConfigError):
        driver.update_point_table(other)
    await driver.close()
