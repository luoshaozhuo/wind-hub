"""三个协议 Driver 的单点/批量公开接口转发契约。

ADS/IEC104 的 read_one/write_one 仍转发到 many 实现；Modbus 的
read_one/write_one 独立实现，write_many 明确不支持。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import (
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.infrastructure.protocol.ads.driver import ADSDriver
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from core.infrastructure.protocol.modbus.mapping import ModbusPoint


@pytest.mark.parametrize("driver_type", [ADSDriver, IEC104Driver])
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
    assert [x.point_id for x in await driver.read(("p2", "p1"))] == ["p2", "p1"]

    if driver_type is ModbusDriver:
        # Modbus 旧 write 接口不允许绕过 write_many 不支持的限制。
        with pytest.raises(NotImplementedError):
            await driver.write((ProtocolWrite("p2", 2),))
    else:
        driver.write_many = fake_write_many
        assert [x.point_id for x in await driver.write((ProtocolWrite("p2", 2),))] == ["p2"]


@pytest.mark.asyncio
async def test_modbus_single_operations_do_not_delegate_to_many() -> None:
    """Modbus read_one/write_one 独立实现，不经过 read_many/write_many。"""
    driver = object.__new__(ModbusDriver)

    async def fail_read_many(ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        raise AssertionError(f"read_one must not delegate to read_many: {ids}")

    async def fail_write_many(
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        raise AssertionError(f"write_one must not delegate to write_many: {writes}")

    driver.read_many = fail_read_many
    driver.write_many = fail_write_many

    driver._lock = asyncio.Lock()
    driver._connected = False
    driver._points = {
        "p1": ModbusPoint(
            point_id="p1",
            register_type="holding",
            address=100,
            count=1,
            data_type="uint16",
            word_order="big_endian",
        )
    }

    # 未连接时在进入 any-many 路径前就失败，证明未委托。
    with pytest.raises(ProtocolError, match="active connection"):
        await driver.read_one("p1")
    with pytest.raises(ProtocolError, match="active connection"):
        await driver.write_one(ProtocolWrite("p1", 1))


@pytest.mark.asyncio
async def test_modbus_write_many_always_not_implemented() -> None:
    driver = object.__new__(ModbusDriver)
    with pytest.raises(NotImplementedError, match="write_many"):
        await driver.write_many(())
    with pytest.raises(NotImplementedError, match="write_many"):
        await driver.write_many((ProtocolWrite("p1", 1),))
