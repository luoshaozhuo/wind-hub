"""站召/组召/计数器召唤：ACT_CON/ACT_TERM、数据按 COT 收集、否定与超时。"""

from __future__ import annotations

import asyncio

import pytest
from hat.drivers.iec101 import common as iec101
from hat.drivers.iec104 import common as iec104

from core.application.errors import ConfigError
from core.application.protocol_contract import InterrogationRequest
from core.domain import PointAccess
from core.infrastructure.protocol.iec104.driver import (
    IEC104InterrogationIncompleteError,
)
from tests.support.iec104_fake import (
    FakeConnection,
    command_msg,
    counter_interrogation_reply,
    interrogation_reply,
    make_driver,
    make_point,
)

pytestmark = pytest.mark.asyncio


def wire(monkeypatch, conn):
    async def _connect(addr, **kwargs):
        del addr, kwargs
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _connect)


async def pump_until(predicate, timeout=1.0):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def q(quality=None):
    return quality or iec101.MeasurementQuality(
        invalid=False,
        not_topical=False,
        substituted=False,
        blocked=False,
        overflow=False,
    )


def float_msg(ca, ioa, value, *, cause):
    return iec101.DataMsg(
        is_test=False,
        originator_address=0,
        asdu_address=ca,
        io_address=ioa,
        data=iec101.FloatingData(iec101.FloatingValue(value), q()),
        time=None,
        cause=cause,
    )


def counter_msg(ca, ioa, value, *, cause=iec101.DataResCause.INTERROGATED_COUNTER):
    return iec101.DataMsg(
        is_test=False,
        originator_address=0,
        asdu_address=ca,
        io_address=ioa,
        data=iec101.BinaryCounterData(iec101.BinaryCounterValue(value), q()),
        time=None,
        cause=cause,
    )


def interrogations(conn):
    out = []
    for m in conn.sent_messages:
        if isinstance(m, iec104.InterrogationMsg | iec104.CounterInterrogationMsg):
            out.append(m)
    return out


async def test_station_interrogation_collects_samples(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100), make_point("b", 101)])
    await driver.connect()

    task = asyncio.create_task(driver.interrogate(InterrogationRequest()))
    await pump_until(lambda: interrogations(conn))
    sent = interrogations(conn)[0]
    assert isinstance(sent, iec104.InterrogationMsg)
    assert sent.request == 20

    conn.deliver(
        [
            interrogation_reply(
                1, request=20, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            ),
            float_msg(1, 100, 1.5, cause=iec101.DataResCause.INTERROGATED_STATION),
            float_msg(1, 101, 2.5, cause=iec101.DataResCause.INTERROGATED_STATION),
            interrogation_reply(1, request=20, cause=iec104.CommandResCause.ACTIVATION_TERMINATION),
        ]
    )
    samples = await asyncio.wait_for(task, 1)
    values = sorted(s.value for s in samples)
    assert values == [1.5, 2.5]
    await driver.close()


async def test_group_interrogation_uses_qoi_20_plus_group(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)])
    await driver.connect()
    task = asyncio.create_task(driver.interrogate(InterrogationRequest(kind="station", group=3)))
    await pump_until(lambda: interrogations(conn))
    assert interrogations(conn)[0].request == 23
    conn.deliver(
        [
            interrogation_reply(
                1, request=23, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            ),
            float_msg(1, 100, 9.0, cause=iec101.DataResCause.INTERROGATED_GROUP03),
            interrogation_reply(1, request=23, cause=iec104.CommandResCause.ACTIVATION_TERMINATION),
        ]
    )
    samples = await asyncio.wait_for(task, 1)
    assert samples[0].value == 9.0
    await driver.close()


async def test_counter_interrogation_collects_integrated_totals(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("kwh", 300, type_id="M_IT_NA_1")])
    await driver.connect()
    task = asyncio.create_task(driver.interrogate(InterrogationRequest(kind="counter")))
    await pump_until(lambda: interrogations(conn))
    sent = interrogations(conn)[0]
    assert isinstance(sent, iec104.CounterInterrogationMsg)
    assert sent.request == 5 and sent.freeze == iec104.FreezeCode.READ

    conn.deliver(
        [
            counter_interrogation_reply(
                1, request=5, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            ),
            counter_msg(1, 300, 42),
            counter_interrogation_reply(
                1, request=5, cause=iec104.CommandResCause.ACTIVATION_TERMINATION
            ),
        ]
    )
    samples = await asyncio.wait_for(task, 1)
    assert samples[0].value == 42
    await driver.close()


async def test_negative_act_con_is_incomplete(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)], options={"t1": 2.0})
    await driver.connect()
    task = asyncio.create_task(driver.interrogate(InterrogationRequest()))
    await pump_until(lambda: interrogations(conn))
    conn.deliver(
        [
            interrogation_reply(
                1, request=20, negative=True, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            )
        ]
    )
    with pytest.raises(IEC104InterrogationIncompleteError):
        await task
    await driver.close()


async def test_timeout_is_incomplete_and_isolates(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)], options={"t1": 0.15, "t2": 0.05})
    await driver.connect()
    with pytest.raises(IEC104InterrogationIncompleteError):
        await driver.interrogate(InterrogationRequest())
    assert not driver.is_open()


async def test_unknown_ca_rejected(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)])
    await driver.connect()
    with pytest.raises(ConfigError):
        await driver.interrogate(InterrogationRequest(common_address=9))
    await driver.close()


async def test_interrogation_and_control_mutex_on_same_ca(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver(
        [
            make_point("a", 100),
            make_point(
                "cmd",
                200,
                type_id="C_SC_NA_1",
                access=PointAccess.WRITE,
                extra={"confirmation": "act_con"},
            ),
        ]
    )
    await driver.connect()
    # GI 先拿到站锁并停在等待 ACT_CON。
    gi = asyncio.create_task(driver.interrogate(InterrogationRequest()))
    await pump_until(lambda: isinstance(conn.last_message(), iec104.InterrogationMsg))
    from core.application.protocol_contract import ProtocolWrite

    write = asyncio.create_task(driver.write_one(ProtocolWrite(point="cmd", value=True)))
    await asyncio.sleep(0.05)
    # GI 未完成前，控制命令不得发出。
    from hat.drivers.iec104 import common as c4

    assert not any(isinstance(m, c4.CommandMsg) for m in conn.sent_messages)

    conn.deliver(
        [
            interrogation_reply(
                1, request=20, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            ),
            interrogation_reply(1, request=20, cause=iec104.CommandResCause.ACTIVATION_TERMINATION),
        ]
    )
    await asyncio.wait_for(gi, 1)
    # GI 释放站锁后，控制命令才发出；回 ACT_CON 使其正常收敛。
    await pump_until(lambda: any(isinstance(m, c4.CommandMsg) for m in conn.sent_messages))
    cmd = next(m for m in conn.sent_messages if isinstance(m, c4.CommandMsg))
    conn.deliver(
        [command_msg(1, 200, cmd.command, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION)]
    )
    result = await asyncio.wait_for(write, 1)
    assert result.outcome.value == "confirmed_success"
    await driver.close()


async def test_spontaneous_data_not_collected_into_interrogation(monkeypatch):
    conn = FakeConnection()
    wire(monkeypatch, conn)
    driver = make_driver([make_point("a", 100)])
    await driver.connect()
    task = asyncio.create_task(driver.interrogate(InterrogationRequest()))
    await pump_until(lambda: interrogations(conn))
    conn.deliver(
        [
            interrogation_reply(
                1, request=20, cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION
            ),
            # SPONTANEOUS 数据不得计入 GI 结果。
            float_msg(1, 100, 7.0, cause=iec101.DataResCause.SPONTANEOUS),
            interrogation_reply(1, request=20, cause=iec104.CommandResCause.ACTIVATION_TERMINATION),
        ]
    )
    samples = await asyncio.wait_for(task, 1)
    assert samples == ()
    await driver.close()
