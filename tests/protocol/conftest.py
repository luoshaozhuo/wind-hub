"""三协议 Adapter 的测试构造与独立服务端生命周期。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from core.domain import ConnectionEndpoint, Point, PointAccess, PointTable, Protocol
from core.domain.identities import BusinessPointId, PointTableId
from core.infrastructure.protocol.iec104 import IEC104Driver
from core.infrastructure.protocol.modbus import ModbusDriver
from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from tests.support.process import free_port
from tests.support.units import NONE_UNIT


def make_point(
    point_id: str,
    *,
    access: PointAccess = PointAccess.READ_WRITE,
    **ext: str | int,
) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext=ext,
    )


def make_table(protocol: str, *points: Point) -> PointTable:
    return PointTable(
        PointTableId("adapter_test"),
        Protocol(protocol),
        {point.point_id: point for point in points},
    )


def modbus_driver(port: int, *, timeout: float = 5.0) -> ModbusDriver:
    table = make_table(
        "modbus",
        make_point("speed", access=PointAccess.READ, register_type="holding", address=100, data_type="float32"),
        make_point("power", access=PointAccess.READ, register_type="holding", address=102, data_type="float32"),
        make_point("command", register_type="holding", address=200, data_type="float32"),
        make_point("temperature", register_type="holding", address=104, data_type="int16"),
    )
    return ModbusDriver(ConnectionEndpoint("127.0.0.1", port), table, {"word_order": "big_endian", "timeout": timeout})


def iec104_driver(port: int) -> IEC104Driver:
    table = make_table(
        "iec104",
        make_point("power", access=PointAccess.READ, ca=1, ioa=100, type_id="M_ME_NC_1"),
        make_point("switch", access=PointAccess.WRITE, ca=1, ioa=1001, type_id="C_SC_NA_1", confirmation="act_con"),
    )
    return IEC104Driver(ConnectionEndpoint("127.0.0.1", port), table, {})


@pytest.fixture
async def modbus_server() -> AsyncIterator[ModbusMockServer]:
    server = ModbusMockServer(port=free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def iec104_server() -> AsyncIterator[IEC104ControlServer]:
    server = IEC104ControlServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()
