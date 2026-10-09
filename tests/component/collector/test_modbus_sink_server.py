"""Modbus TCP Sink Server component test（真实 pymodbus 主站 × 新栈 Sink Server）。

单元测试（tests/unit/collector/sink/test_modbus_sink.py）不建立网络连接；
本文件覆盖其无法替代的真实 wire 语义：外部主站经 TCP 读活寄存器/线圈、
写请求被拒绝、未声明地址返回异常响应、store 更新不经传输层拷贝即对主站可见。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pymodbus.client import AsyncModbusTcpClient

from collector.domain.point_value import PointValue
from collector.infrastructure.sink.modbus.pipeline import ModbusSinkDataPath
from collector.infrastructure.sink.modbus.server import ModbusTcpSinkServer
from core.application.sink_config import (
    ModbusSinkAddress,
    ModbusSinkConnection,
    ResolvedSinkPoint,
    SinkSource,
)
from tests.support.process import free_port

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
async def server() -> AsyncIterator[
    tuple[ModbusTcpSinkServer, ModbusSinkDataPath, int]
]:
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
        yield tcp, path, port
    finally:
        await tcp.stop()


async def test_client_reads_live_store_and_writes_are_rejected(
    server,  # type: ignore[no-untyped-def]
) -> None:
    tcp, _, port = server
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


async def test_unknown_address_returns_exception(
    server,  # type: ignore[no-untyped-def]
) -> None:
    _, _, port = server
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    assert await client.connect()
    try:
        response = await client.read_holding_registers(500, count=1, device_id=1)
        assert response.isError()
    finally:
        client.close()


async def test_server_reads_store_updates_without_transport_copy(
    server,  # type: ignore[no-untyped-def]
) -> None:
    _, path, port = server
    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    assert await client.connect()
    try:
        before = await client.read_holding_registers(100, count=2, device_id=1)
        assert list(before.registers) == [0x3F80, 0x0000]

        path.update([PointValue(device_id="wt01", point_id="power", value=2.0)])

        after = await client.read_holding_registers(100, count=2, device_id=1)
        assert not after.isError()
        assert list(after.registers) == [0x4000, 0x0000]
    finally:
        client.close()
