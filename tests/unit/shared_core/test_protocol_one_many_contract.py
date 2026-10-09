"""三个协议 Driver 的单点/批量公开接口转发契约。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from core.application.protocol_contract import (
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.infrastructure.protocol.ads.driver import ADSDriver
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.modbus.driver import ModbusDriver


@pytest.mark.parametrize("driver_type", [ModbusDriver, ADSDriver, IEC104Driver])
@pytest.mark.asyncio
async def test_single_operations_forward_to_many(driver_type: type) -> None:
    driver = object.__new__(driver_type)
    read_calls: list[tuple[str, ...]] = []
    write_calls: list[tuple[ProtocolWrite, ...]] = []

    async def fake_read_many(ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        read_calls.append(tuple(ids))
        return tuple(ProtocolSample(point_id=i, value=3, quality=Quality.GOOD) for i in ids)

    async def fake_write_many(
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        write_calls.append(tuple(writes))
        return tuple(ProtocolWriteResult(point_id=w.point_id, success=True) for w in writes)

    driver.read_many = fake_read_many
    driver.write_many = fake_write_many

    assert (await driver.read_one("p1")).point_id == "p1"
    command = ProtocolWrite("p1", 5)
    assert (await driver.write_one(command)).success
    assert read_calls == [("p1",)]
    assert write_calls == [(command,)]


@pytest.mark.parametrize("driver_type", [ModbusDriver, ADSDriver, IEC104Driver])
@pytest.mark.asyncio
async def test_legacy_operations_forward_to_many(driver_type: type) -> None:
    driver = object.__new__(driver_type)

    async def fake_read_many(ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        return tuple(ProtocolSample(point_id=i, value=1) for i in ids)

    async def fake_write_many(
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        return tuple(ProtocolWriteResult(point_id=w.point_id, success=True) for w in writes)

    driver.read_many = fake_read_many
    driver.write_many = fake_write_many

    assert [x.point_id for x in await driver.read(("p2", "p1"))] == ["p2", "p1"]
    assert [x.point_id for x in await driver.write((ProtocolWrite("p2", 2),))] == ["p2"]
