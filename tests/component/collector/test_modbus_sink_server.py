"""Modbus TCP Sink Server component test。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pymodbus.client import AsyncModbusTcpClient

from tests.support.process import free_port
from wind_hub_collector.adapter.outbound.sink.modbus_pipeline import ModbusSinkDataPath
from wind_hub_collector.adapter.outbound.sink.modbus_server import ModbusTcpSinkServer
from wind_hub_core.config.sinks import (
    ModbusSinkAddress,
    ModbusSinkConnection,
    ResolvedSinkPoint,
    SinkSource,
)
from wind_hub_core.model.point import PointValue

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


def _point(
    point_id: str,
    *,
    datatype: str,
    register_type: str,
    address: int,
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id=point_id),
        ref=f"wt01.{point_id}",
        source_data_type=datatype,
        source_unit="none",
        datatype=datatype,
        unit="none",
        address=ModbusSinkAddress(
            unit_id=1,
            register_type=register_type,  # type: ignore[arg-type]
            address=address,
        ),
    )


@pytest.fixture
async def server() -> AsyncIterator[tuple[ModbusTcpSinkServer, int]]:
    port = free_port()
    path = ModbusSinkDataPath(
        [
            _point("power", datatype="float32", register_type="holding", address=100),
            _point("running", datatype="bool", register_type="coil", address=10),
        ]
    )
    path.update(
        [
            PointValue(device_id="wt01", point_id="power", value=1.0),
            PointValue(device_id="wt01", point_id="running", value=True),
        ]
    )
    tcp = ModbusTcpSinkServer(
        ModbusSinkConnection(host="127.0.0.1", port=port),
        path,
    )
    await tcp.start()
    try:
        yield tcp, port
    finally:
        await tcp.stop()


async def test_client_reads_live_store_and_writes_are_rejected(server) -> None:  # type: ignore[no-untyped-def]
    tcp, port = server
    assert tcp.health().healthy is True

    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    assert await client.connect()
    try:
        hr = await client.read_holding_registers(100, count=2, device_id=1)
        assert not hr.isError()
        assert list(hr.registers) == [0x3F80, 0x0000]

        coils = await client.read_coils(10, count=1, device_id=1)
        assert not coils.isError()
        assert coils.bits[0] is True

        rejected = await client.write_register(100, 7, device_id=1)
        assert rejected.isError()
    finally:
        client.close()


async def test_unknown_address_returns_exception(server) -> None:  # type: ignore[no-untyped-def]
    _, port = server
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    assert await client.connect()
    try:
        response = await client.read_holding_registers(500, count=1, device_id=1)
        assert response.isError()
    finally:
        client.close()
