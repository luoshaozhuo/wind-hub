"""三个协议 Driver 的单点/批量公开接口契约。

所有协议的 read_one/write_one 均为独立实现，不经过 read_many/write_many；
Modbus write_many 明确不支持。
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
)
from core.infrastructure.protocol.ads.driver import ADSDriver
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from core.infrastructure.protocol.modbus.mapping import ModbusPoint


def _fail_many(driver: object) -> None:
    async def fail_read_many(ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        raise AssertionError(f"read_one must not delegate to read_many: {ids}")

    async def fail_write_many(
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        raise AssertionError(f"write_one must not delegate to write_many: {writes}")

    driver.read_many = fail_read_many  # type: ignore[attr-defined]
    driver.write_many = fail_write_many  # type: ignore[attr-defined]


def _bare_modbus_driver() -> ModbusDriver:
    driver = object.__new__(ModbusDriver)
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
    return driver


def _bare_ads_driver() -> ADSDriver:
    driver = object.__new__(ADSDriver)
    driver._lock = asyncio.Lock()
    driver._connected = False
    driver._connection = None
    driver._mapped_point = lambda point_id: object()  # type: ignore[method-assign, assignment]
    return driver


def _bare_iec104_driver() -> IEC104Driver:
    driver = object.__new__(IEC104Driver)
    driver._is_open = False
    driver._station = None
    return driver


@pytest.mark.asyncio
async def test_modbus_single_operations_do_not_delegate_to_many() -> None:
    """Modbus read_one/write_one 独立实现，不经过 read_many/write_many。"""
    driver = _bare_modbus_driver()
    _fail_many(driver)

    # 未连接时在进入 any-many 路径前就失败，证明未委托。
    with pytest.raises(ProtocolError, match="active connection"):
        await driver.read_one("p1")
    with pytest.raises(ProtocolError, match="active connection"):
        await driver.write_one(ProtocolWrite("p1", 1))


@pytest.mark.asyncio
async def test_ads_single_operations_do_not_delegate_to_many() -> None:
    """ADS read_one/write_one 独立实现，不经过 read_many/write_many。"""
    driver = _bare_ads_driver()
    _fail_many(driver)

    with pytest.raises(ProtocolError, match="active connection"):
        await driver.read_one("p1")
    with pytest.raises(ProtocolError, match="active connection"):
        await driver.write_one(ProtocolWrite("p1", 1))


@pytest.mark.asyncio
async def test_iec104_single_operations_do_not_delegate_to_many() -> None:
    """IEC104 read_one/write_one 独立实现，不经过 read_many/write_many。"""
    driver = _bare_iec104_driver()
    _fail_many(driver)

    with pytest.raises(ProtocolError, match="OPEN connection"):
        await driver.read_one("p1")
    with pytest.raises(ProtocolError, match="OPEN connection"):
        await driver.write_one(ProtocolWrite("p1", 1))


@pytest.mark.asyncio
async def test_modbus_write_many_always_not_implemented() -> None:
    driver = object.__new__(ModbusDriver)
    with pytest.raises(NotImplementedError, match="write_many"):
        await driver.write_many(())
    with pytest.raises(NotImplementedError, match="write_many"):
        await driver.write_many((ProtocolWrite("p1", 1),))
