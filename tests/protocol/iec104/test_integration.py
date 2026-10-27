"""真实 c104 从站与 hat IEC104 客户端交互。"""

from __future__ import annotations

import asyncio

import pytest

from core.application.protocol_contract import InterrogationRequest, ProtocolWrite, Quality, WriteOutcome
from tests.protocol.conftest import iec104_driver


@pytest.mark.asyncio
async def test_iec104_active_read_and_interrogation(iec104_server) -> None:
    driver = iec104_driver(iec104_server.port)
    await driver.connect()
    try:
        sample = await driver.read_one("power")
        assert sample.point_id == "power"
        assert sample.value == pytest.approx(1500.5)
        assert sample.quality is Quality.GOOD
        samples = await driver.interrogate(InterrogationRequest(kind="station", common_address=1))
        assert any(sample.point_id == "power" for sample in samples)
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_iec104_direct_control_reaches_server(iec104_server) -> None:
    driver = iec104_driver(iec104_server.port)
    await driver.connect()
    try:
        reply = await driver.write_one(ProtocolWrite("switch", True))
        assert reply.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert iec104_server.last_control.get("C_SC_NA_1") == 1001
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_iec104_spontaneous_listen(iec104_server) -> None:
    driver = iec104_driver(iec104_server.port)
    received = asyncio.Event()
    samples = []

    async def callback(batch) -> None:
        samples.extend(batch)
        received.set()

    await driver.connect()
    listener = await driver.listen(["power"], callback)
    try:
        await iec104_server.set_value(100, 1700.0, spontaneous=True)
        await asyncio.wait_for(received.wait(), timeout=5.0)
        assert any(sample.point_id == "power" and sample.value == pytest.approx(1700.0) for sample in samples)
    finally:
        await listener.close()
        await driver.close()


@pytest.mark.asyncio
async def test_iec104_select_before_execute() -> None:
    """必须完成 SELECT 确认后才允许 EXECUTE。"""
    from core.domain import ConnectionEndpoint, PointAccess
    from core.infrastructure.protocol.iec104 import IEC104Driver
    from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
    from tests.protocol.conftest import make_point, make_table

    server = IEC104ControlServer(sbo_ioas={1001})
    driver = IEC104Driver(
        ConnectionEndpoint("127.0.0.1", server.port),
        make_table(
            "iec104",
            make_point(
                "switch",
                access=PointAccess.WRITE,
                ca=1,
                ioa=1001,
                type_id="C_SC_NA_1",
                command_mode="select_execute",
                confirmation="act_con",
            ),
        ),
        {},
    )
    await server.start()
    try:
        await driver.connect()
        result = await driver.write_one(ProtocolWrite("switch", True))
        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
        assert server.last_control.get("C_SC_NA_1") == 1001
    finally:
        await driver.close()
        await server.stop()


@pytest.mark.asyncio
async def test_iec104_negative_control_confirmation_is_definite_failure() -> None:
    from tests.fixtures.servers.iec104_control_server import IEC104ControlServer

    server = IEC104ControlServer(reject_ioas={1001})
    driver = iec104_driver(server.port)
    await server.start()
    try:
        await driver.connect()
        result = await driver.write_one(ProtocolWrite("switch", True))
        assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
        assert server.last_control.get("C_SC_NA_1") == 1001
    finally:
        await driver.close()
        await server.stop()
