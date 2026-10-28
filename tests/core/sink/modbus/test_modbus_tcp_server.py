"""Modbus Sink TCP Server 测试。

- StoreBackedModbusContext：四个只读功能码直通 store、非法功能码与
  ILLEGAL_ADDRESS 映射、写功能码一律 ILLEGAL_FUNCTION；
- ModbusTcpSinkServer：真实 pymodbus loopback 主站读 fc1/fc3，写 fc6 被拒，
  生命周期幂等且端口释放。仅使用 127.0.0.1 回环。
"""

from __future__ import annotations

import socket

import pytest
from pymodbus.constants import ExcCodes

from core.application import Quality
from core.application.sink_contract import (
    ModbusSinkAddress,
    ResolvedSinkPoint,
    SinkSource,
)
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import ModbusSinkConnection
from core.infrastructure.sink.modbus.pipeline import ModbusSinkDataPath
from core.infrastructure.sink.modbus.server import (
    ModbusTcpSinkServer,
    StoreBackedModbusContext,
)

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


def _point(
    *,
    ref: str = "scada.wind_speed",
    slave_id: int = 1,
    register_type: str = "holding",
    address: int = 10,
    datatype: str = "uint16",
) -> ResolvedSinkPoint:
    return ResolvedSinkPoint(
        source=SinkSource(device_id="wt01", point_id=ref),
        ref=ref,
        source_data_type="uint16",
        source_unit="none",
        datatype=datatype,
        unit="none",
        address=ModbusSinkAddress(slave_id=slave_id, register_type=register_type, address=address),
    )


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------
# StoreBackedModbusContext 纯适配层
# --------------------------------------------------------------------------


async def test_context_read_functions_map_to_store() -> None:
    path = ModbusSinkDataPath(
        [
            _point(ref="a", register_type="holding", address=10, datatype="uint16"),
            _point(ref="b", register_type="coil", address=5, datatype="bool"),
            _point(ref="c", register_type="discrete", address=6, datatype="bool"),
            _point(ref="d", register_type="input", address=0, datatype="uint16"),
        ]
    )
    context = StoreBackedModbusContext(path.store)

    assert await context.async_getValues(1, 3, 10, 1) == [0]
    assert await context.async_getValues(1, 4, 0, 1) == [0]
    assert await context.async_getValues(1, 1, 5, 1) == [False]
    assert await context.async_getValues(1, 2, 6, 1) == [False]
    assert context.device_ids() == [1]


async def test_context_unknown_function_and_address() -> None:
    path = ModbusSinkDataPath([_point()])
    context = StoreBackedModbusContext(path.store)

    # fc6（写单寄存器）不是声明的只读功能码。
    assert await context.async_getValues(1, 6, 10, 1) is ExcCodes.ILLEGAL_FUNCTION
    # slave 2 未声明 → 地址非法。
    assert await context.async_getValues(2, 3, 10, 1) is ExcCodes.ILLEGAL_ADDRESS


async def test_context_rejects_all_writes() -> None:
    path = ModbusSinkDataPath([_point()])
    context = StoreBackedModbusContext(path.store)

    result = await context.async_setValues(1, 6, 10, [1])
    assert result is ExcCodes.ILLEGAL_FUNCTION


# --------------------------------------------------------------------------
# 真实 loopback TCP 生命周期
# --------------------------------------------------------------------------


async def test_server_serves_reads_rejects_writes_and_releases_port() -> None:
    from pymodbus.client import AsyncModbusTcpClient

    port = _free_port()
    path = ModbusSinkDataPath(
        [
            _point(ref="a", register_type="holding", address=10, datatype="uint16"),
            _point(ref="b", register_type="coil", address=5, datatype="bool"),
        ]
    )
    # 通过内部数据路径写入一个 GOOD 值，验证 Server 读取同一份内存。
    path.update([PointValue(device_id="wt01", point_id="a", value=0x4321, quality=Quality.GOOD)])
    connection = ModbusSinkConnection(host="127.0.0.1", port=port)
    server = ModbusTcpSinkServer(connection, path)

    assert (await server.health()).healthy is False
    await server.start()
    await server.start()  # 重复启动安全
    assert (await server.health()).healthy is True
    assert server.port == port

    client = AsyncModbusTcpClient(host="127.0.0.1", port=port)
    await client.connect()
    try:
        rr = await client.read_holding_registers(10, count=1, device_id=1)
        assert not rr.isError()
        assert rr.registers == [0x4321]

        rc = await client.read_coils(5, count=1, device_id=1)
        assert not rc.isError()
        assert rc.bits[:1] == [False]

        # 只读 Sink：主站写必须返回 ILLEGAL FUNCTION。
        wr = await client.write_register(10, 1, device_id=1)
        assert wr.isError()
        assert wr.exception_code == ExcCodes.ILLEGAL_FUNCTION.value
    finally:
        client.close()
        await server.stop()
        await server.stop()  # 重复停止安全

    assert (await server.health()).healthy is False
    assert (await server.health()).message == "not listening"
