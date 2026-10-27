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
