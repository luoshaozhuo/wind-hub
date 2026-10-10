"""三个协议 Driver 的单点/批量公开接口契约。

所有协议的 read_one/write_one 均为独立实现，不经过 read_many/write_many；
Modbus write_many 明确不支持。

每个协议同时覆盖：

- 未连接时的提前失败路径；
- **连接成功的正常传输路径**（mock wire 客户端），断言单点操作真实完成
  一次协议读写，且 read_many/write_many 全程未被调用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import (
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.ads.driver import ADSDriver
from core.infrastructure.protocol.ads.mapping import ADSPoint
from core.infrastructure.protocol.iec104.driver import IEC104Driver
from core.infrastructure.protocol.iec104.mapping import IEC104Point
from core.infrastructure.protocol.modbus.config import parse_modbus_config
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


# ---------------------------------------------------------------------------
# Modbus：mock pymodbus client 的成功路径
# ---------------------------------------------------------------------------


class _ModbusResponse:
    def __init__(self, *, bits: list[bool] | None = None, registers: list[int] | None = None):
        self.bits = bits or []
        self.registers = registers or []

    def isError(self) -> bool:  # noqa: N802 - pymodbus response API
        return False


class _ModbusClient:
    """记录真实 wire 调用的最小 pymodbus client 替身。"""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, int, int]] = []
        self.write_calls: list[tuple[str, int, object]] = []
        self.connected = True

    def close(self) -> None:
        self.connected = False

    async def read_coils(self, address: int, *, count: int, device_id: int) -> _ModbusResponse:
        self.read_calls.append(("coil", address, count))
        return _ModbusResponse(bits=[True] * count)

    async def read_discrete_inputs(
        self, address: int, *, count: int, device_id: int
    ) -> _ModbusResponse:
        self.read_calls.append(("discrete_input", address, count))
        return _ModbusResponse(bits=[True] * count)

    async def read_holding_registers(
        self, address: int, *, count: int, device_id: int
    ) -> _ModbusResponse:
        self.read_calls.append(("holding", address, count))
        return _ModbusResponse(registers=[7] * count)

    async def read_input_registers(
        self, address: int, *, count: int, device_id: int
    ) -> _ModbusResponse:
        self.read_calls.append(("input", address, count))
        return _ModbusResponse(registers=[7] * count)

    async def write_coil(self, address: int, value: bool, *, device_id: int) -> _ModbusResponse:
        self.write_calls.append(("coil", address, value))
        return _ModbusResponse()

    async def write_register(self, address: int, value: int, *, device_id: int) -> _ModbusResponse:
        self.write_calls.append(("register", address, value))
        return _ModbusResponse()

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int
    ) -> _ModbusResponse:
        self.write_calls.append(("registers", address, values))
        return _ModbusResponse()


def _modbus_point(point_id: str) -> ModbusPoint:
    return ModbusPoint(
        point_id=point_id,
        register_type="holding",
        address=100,
        count=1,
        data_type="uint16",
        word_order="big_endian",
    )


def _connected_modbus_driver(client: _ModbusClient) -> ModbusDriver:
    driver = object.__new__(ModbusDriver)
    driver._lock = asyncio.Lock()
    driver._config = parse_modbus_config(ConnectionEndpoint("192.0.2.10", 502), {})
    driver._points = {"p1": _modbus_point("p1")}
    driver._client = client
    driver._connected = True
    return driver


def _bare_modbus_driver() -> ModbusDriver:
    driver = _connected_modbus_driver(_ModbusClient())
    driver._connected = False
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
async def test_modbus_single_operations_succeed_without_many_on_connected_path() -> None:
    """连接成功路径：read_one/write_one 各完成一次真实 Modbus 请求。"""
    client = _ModbusClient()
    driver = _connected_modbus_driver(client)
    _fail_many(driver)

    sample = await driver.read_one("p1")
    assert client.read_calls == [("holding", 100, 1)]
    assert sample.value == 7
    assert sample.quality == Quality.GOOD

    result = await driver.write_one(ProtocolWrite("p1", 42))
    assert result.success
    assert client.write_calls == [("register", 100, 42)]


# ---------------------------------------------------------------------------
# ADS：mock pyads connection 的成功路径
# ---------------------------------------------------------------------------


class _ADSConnection:
    """记录真实 wire 调用的最小 pyads Connection 替身（同步 API）。"""

    def __init__(self) -> None:
        self.read_calls: list[tuple[int, int, Any]] = []
        self.write_calls: list[tuple[int, int, object, Any]] = []

    def read(self, index_group: int, index_offset: int, datatype: Any) -> object:
        self.read_calls.append((index_group, index_offset, datatype))
        return 123

    def write(self, index_group: int, index_offset: int, value: object, datatype: Any) -> None:
        self.write_calls.append((index_group, index_offset, value, datatype))


def _ads_point(point_id: str) -> ADSPoint:
    return ADSPoint(
        point_id=point_id,
        index_group=0xF003,
        index_offset=0x1000,
        data_type="INT",
        size=2,
        symbol=None,
        address_resolved=True,
    )


def _connected_ads_driver(connection: _ADSConnection) -> ADSDriver:
    driver = object.__new__(ADSDriver)
    driver._lock = asyncio.Lock()
    driver._points = {"p1": _ads_point("p1")}
    driver._connection = connection
    driver._connected = True
    return driver


def _bare_ads_driver() -> ADSDriver:
    driver = _connected_ads_driver(_ADSConnection())
    driver._connected = False
    driver._connection = None
    return driver


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
async def test_ads_single_operations_succeed_without_many_on_connected_path() -> None:
    """连接成功路径：read_one/write_one 各完成一次真实 ADS 请求。"""
    connection = _ADSConnection()
    driver = _connected_ads_driver(connection)
    _fail_many(driver)

    sample = await driver.read_one("p1")
    assert len(connection.read_calls) == 1
    index_group, index_offset, _ = connection.read_calls[0]
    assert (index_group, index_offset) == (0xF003, 0x1000)
    assert sample.value == 123
    assert sample.quality == Quality.GOOD

    result = await driver.write_one(ProtocolWrite("p1", 5))
    assert result.success
    assert len(connection.write_calls) == 1
    _, _, value, _ = connection.write_calls[0]
    assert value == 5


# ---------------------------------------------------------------------------
# IEC104：镜像读取 + 命令发送的成功路径
# ---------------------------------------------------------------------------


class _IEC104CommandPoint:
    """记录 transmit 调用的最小 c104 命令点替身（同步 API）。"""

    def __init__(self, point_type: Any) -> None:
        self.type = point_type
        self.value: object = None
        self.transmit_calls: list[Any] = []

    def transmit(self, cot: Any) -> bool:
        self.transmit_calls.append(cot)
        return True


class _IEC104Station:
    def __init__(self, point: _IEC104CommandPoint) -> None:
        self._point = point

    def get_point(self, ioa: int) -> _IEC104CommandPoint:
        return self._point


def _connected_iec104_driver() -> IEC104Driver:
    import c104

    driver = object.__new__(IEC104Driver)
    driver._is_open = True
    driver._points_by_id = {"p1": IEC104Point(point_id="p1", ioa=100, type_id="C_SC_NA_1")}
    driver._samples = {
        100: ProtocolSample(
            point_id="p1",
            value=True,
            quality=Quality.GOOD,
            timestamp=datetime.now(UTC),
        ),
    }
    driver._command_locks = {}
    command_point = _IEC104CommandPoint(c104.Type.C_SC_NA_1)
    driver._station = _IEC104Station(command_point)
    return driver


def _bare_iec104_driver() -> IEC104Driver:
    driver = _connected_iec104_driver()
    driver._is_open = False
    driver._station = None
    return driver


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
async def test_iec104_single_operations_succeed_without_many_on_open_path() -> None:
    """OPEN 路径：read_one 返回镜像样本；write_one 发送一次真实遥控命令。"""
    driver = _connected_iec104_driver()
    _fail_many(driver)

    sample = await driver.read_one("p1")
    assert sample.value is True
    assert sample.quality == Quality.GOOD

    result = await driver.write_one(ProtocolWrite("p1", True))
    assert result.success
    command_point = driver._station.get_point(100)
    assert len(command_point.transmit_calls) == 1
    assert command_point.value is True


# ---------------------------------------------------------------------------
# Modbus write_many 明确不支持
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modbus_write_many_always_not_implemented() -> None:
    driver = object.__new__(ModbusDriver)
    with pytest.raises(NotImplementedError, match="write_many"):
        await driver.write_many(())
    with pytest.raises(NotImplementedError, match="write_many"):
        await driver.write_many((ProtocolWrite("p1", 1),))
