"""三个协议 Driver 的单点/批量公开接口契约。

所有协议的 read_one/write_one 均为独立实现，不经过 read_many/write_many；
Modbus write_many 明确不支持，IEC104 read_many/write_many 明确不支持。

每个协议同时覆盖：

- 未连接时的提前失败路径；
- **连接成功的正常传输路径**（fake wire 客户端），断言单点操作真实完成
  一次协议读写，且 read_many/write_many 全程未被调用。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from hat.drivers.iec101 import common as iec101

from core.application.errors import ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import (
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WriteOutcome,
)
from core.domain import ConnectionEndpoint
from core.infrastructure.protocol.ads.driver import ADSDriver, ADSState
from core.infrastructure.protocol.ads.mapping import ADSPoint
from core.infrastructure.protocol.modbus.config import parse_modbus_config
from core.infrastructure.protocol.modbus.driver import ModbusDriver
from core.infrastructure.protocol.modbus.mapping import ModbusPoint
from tests.support.iec104_hat_fake import (
    command_confirmation_msg as command_confirmation_iec104,
)
from tests.support.iec104_hat_fake import (
    connected_driver as connected_iec104_driver,
)
from tests.support.iec104_hat_fake import (
    floating_data_msg as floating_data_iec104,
)
from tests.support.iec104_hat_fake import (
    make_driver as make_iec104_driver,
)
from tests.support.iec104_hat_fake import (
    make_point as make_iec104_point,
)


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
# Modbus：fake pymodbus client 的成功路径
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
    driver._health_point_id = "p1"
    driver._client = client
    client.connected = True
    return driver


def _bare_modbus_driver() -> ModbusDriver:
    driver = _connected_modbus_driver(_ModbusClient())
    driver._client.connected = False
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
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert client.write_calls == [("register", 100, 42)]


# -
# ADS：单点即单元素 Sum Read/Write（原生批量原语）
# -


class _ADSConnection:
    """记录 Sum Read/Write wire 调用的最小 pyads Connection 替身。"""

    def __init__(self) -> None:
        self.sum_read_calls: list[list[tuple[int, int, int]]] = []
        self.sum_write_payloads: list[bytes] = []

    def read_write(
        self,
        index_group: int,
        index_offset: int,
        plc_read_datatype: object,
        value: object,
        plc_write_datatype: object,
        return_ctypes: bool = False,
        check_length: bool = True,
    ) -> bytes:
        import struct

        from pyads.constants import ADSIGRP_SUMUP_READ, ADSIGRP_SUMUP_WRITE

        if index_group == ADSIGRP_SUMUP_READ:
            items = [(item.iGroup, item.iOffset, item.size) for item in value]
            self.sum_read_calls.append(items)
            assert items == [(0xF003, 0x1000, 2)]
            return struct.pack("<Ih", 0, 123)
        assert index_group == ADSIGRP_SUMUP_WRITE
        raw = bytes(value)
        self.sum_write_payloads.append(raw)
        return b"\x00" * (4 * index_offset)


def _ads_point(point_id: str) -> ADSPoint:
    return ADSPoint(
        point_id=point_id,
        index_group=0xF003,
        index_offset=0x1000,
        data_type="INT",
        size=2,
        symbol=None,
    )


def _connected_ads_driver(connection: _ADSConnection) -> ADSDriver:
    from core.infrastructure.protocol.ads.session import _AdsSession

    driver = object.__new__(ADSDriver)
    driver._points = {"p1": _ads_point("p1")}
    driver._session = _AdsSession(lambda: connection)
    driver._session._opened = True
    driver._state = ADSState.CONNECTED
    driver._config = SimpleNamespace(max_subs_per_sum=500)
    return driver


def _bare_ads_driver() -> ADSDriver:
    driver = object.__new__(ADSDriver)
    driver._points = {"p1": _ads_point("p1")}
    driver._session = None
    driver._state = ADSState.DISCONNECTED
    driver._config = SimpleNamespace(max_subs_per_sum=500)
    return driver


@pytest.mark.asyncio
async def test_ads_single_operations_fail_fast_without_active_connection() -> None:
    """未连接时单点读写在提交任何 wire 请求前失败。"""
    driver = _bare_ads_driver()

    with pytest.raises(ProtocolError, match="not connected"):
        await driver.read_one("p1")
    with pytest.raises(ProtocolError, match="not connected"):
        await driver.write_one(ProtocolWrite("p1", 1))


@pytest.mark.asyncio
async def test_ads_single_operations_use_one_element_sum_requests() -> None:
    """连接路径：单点读=单元素 Sum Read，单点写=单元素 Sum Write。"""
    connection = _ADSConnection()
    driver = _connected_ads_driver(connection)

    sample = await driver.read_one("p1")
    assert len(connection.sum_read_calls) == 1
    assert sample.value == 123
    assert sample.quality == Quality.GOOD

    result = await driver.write_one(ProtocolWrite("p1", 5))
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(connection.sum_write_payloads) == 1
    # 请求头携带 p1 的 index 地址和 2 字节数据长度。
    assert connection.sum_write_payloads[0].startswith(
        struct.pack("<III", 0xF003, 0x1000, 2)
    )


# ---------------------------------------------------------------------------
# IEC104：主动读 + 命令发送的成功路径
# ---------------------------------------------------------------------------


def _iec104_points() -> list[Any]:
    from core.domain import PointAccess

    return [
        make_iec104_point("p1", 100),
        make_iec104_point("c1", 200, type_id="C_SC_NA_1", access=PointAccess.WRITE),
    ]


@pytest.mark.asyncio
async def test_iec104_single_operations_do_not_delegate_to_many() -> None:
    """IEC104 read_one/write_one 独立实现，不经过 read_many/write_many。"""
    driver, _conn = make_iec104_driver(_iec104_points())
    _fail_many(driver)

    with pytest.raises(ProtocolError, match="OPEN"):
        await driver.read_one("p1")
    with pytest.raises(ProtocolError, match="OPEN"):
        await driver.write_one(ProtocolWrite("c1", True))
    await driver.close()


@pytest.mark.asyncio
async def test_iec104_single_operations_succeed_without_many_on_open_path() -> None:
    """OPEN 路径：read_one 完成一次真实 C_RD 关联；write_one 发送一帧真实命令。

    上下行走 hat 生产编解码字节管线（内存 APCI 假连接），read_many/
    write_many 全程不得被调用。
    """
    driver, conn = await connected_iec104_driver(
        _iec104_points(), options={"t1": 2.0}
    )
    _fail_many(driver)

    read_task = asyncio.create_task(driver.read_one("p1"))
    await asyncio.sleep(0.05)
    conn.deliver(
        [floating_data_iec104(1, 100, 7.5, cause=iec101.DataResCause.REQUEST)]
    )
    sample = await asyncio.wait_for(read_task, timeout=2)
    assert sample.value == 7.5
    assert sample.quality == Quality.GOOD
    assert isinstance(conn.last_message(), iec101.ReadMsg)

    write_task = asyncio.create_task(
        driver.write_one(ProtocolWrite("c1", True))
    )
    await asyncio.sleep(0.05)
    conn.deliver([command_confirmation_iec104(1, 200)])
    assert not write_task.done()
    conn.deliver(
        [
            command_confirmation_iec104(
                1, 200, cause=iec101.CommandResCause.ACTIVATION_TERMINATION
            )
        ]
    )
    result = await asyncio.wait_for(write_task, timeout=2)
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    await driver.close()


# ---------------------------------------------------------------------------
# Modbus write_many 明确不支持
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modbus_write_many_always_not_implemented() -> None:
    driver = object.__new__(ModbusDriver)
    with pytest.raises(ProtocolCapabilityError, match="write_many"):
        await driver.write_many(())
    with pytest.raises(ProtocolCapabilityError, match="write_many"):
        await driver.write_many((ProtocolWrite("p1", 1),))
