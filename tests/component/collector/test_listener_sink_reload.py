"""监听型 Sink 真实端口热重载 component test。"""

from __future__ import annotations

import asyncio
import socket

import pytest
from pymodbus.client import AsyncModbusTcpClient

from tests.support.iec104_master import IEC104MasterClient
from tests.support.process import free_port
from wind_hub_collector.adapter.outbound.sink.iec104 import IEC104Sink
from wind_hub_collector.adapter.outbound.sink.modbus import ModbusSink
from wind_hub_collector.application.runtime import CollectorRuntime
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config import ResolvedSinkConfig, RuntimeConfig
from wind_hub_core.model.point import PointValue

pytestmark = pytest.mark.real_service


def _runtime(sink_name: str, sink) -> CollectorRuntime:  # type: ignore[no-untyped-def]
    return CollectorRuntime(
        devices={},
        sinks={sink_name: sink},
        engine=AcquisitionEngine(read_timeout=None),
        config=RuntimeConfig(
            shutdown_timeout=1.0,
            connect_timeout=0.5,
            read_timeout=0.5,
        ),
        tasks={},
    )


def _modbus_config(port: int) -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name="modbus_scada",
        type="modbus",
        connection={"host": "127.0.0.1", "port": port},
        points=[
            {
                "source": {"device_id": "wt01", "point_id": "power"},
                "ref": "wt01.power",
                "source_data_type": "float32",
                "source_unit": "none",
                "datatype": "float32",
                "unit": "none",
                "address": {
                    "unit_id": 1,
                    "register_type": "holding",
                    "address": 100,
                },
            }
        ],
    )


def _iec104_config(port: int) -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name="iec104_scada",
        type="iec104",
        connection={
            "host": "127.0.0.1",
            "port": port,
            "common_address": 1,
        },
        points=[
            {
                "source": {"device_id": "wt01", "point_id": "power"},
                "ref": "wt01.power",
                "source_data_type": "float32",
                "source_unit": "none",
                "datatype": "float32",
                "unit": "none",
                "address": {"ioa": 1001, "type_id": "M_ME_NC_1"},
            }
        ],
    )


async def test_modbus_runtime_rebuild_reuses_same_listener_port() -> None:
    port = free_port()
    cfg = _modbus_config(port)
    old_sink = ModbusSink(cfg)
    await old_sink.write(
        [PointValue(device_id="wt01", point_id="power", value=1.0)]
    )
    rt = _runtime("modbus_scada", old_sink)
    await rt.start()
    try:
        new_sink = ModbusSink(cfg)
        await new_sink.write(
            [PointValue(device_id="wt01", point_id="power", value=2.0)]
        )
        await rt.sink_runtime.rebuild_sink("modbus_scada", cfg, new_sink)

        client = AsyncModbusTcpClient("127.0.0.1", port=port)
        assert await client.connect()
        try:
            response = await client.read_holding_registers(
                100, count=2, device_id=1
            )
            assert not response.isError()
            assert list(response.registers) == [0x4000, 0x0000]
        finally:
            client.close()
        assert rt.sinks["modbus_scada"] is new_sink
        assert new_sink.health().healthy is True
    finally:
        await rt.stop()


async def _interrogate_values(port: int) -> dict[int, object]:
    """以真实 IEC104 主站（c104）总召，返回 {ioa: value}。

    ACT_CON/ACT_TERM 的 wire 序列由 lib60870-C 保证，这里只验证
    wind-hub 的值链路（exporter→c104 从站点→wire→主站镜像）。
    """
    client = IEC104MasterClient(port)
    await client.connect()
    try:
        return await client.interrogate(expected=1)
    finally:
        await client.close()


async def test_iec104_runtime_rebuild_reuses_same_listener_port() -> None:
    port = free_port()
    cfg = _iec104_config(port)
    old_sink = IEC104Sink(cfg)
    await old_sink.write(
        [PointValue(device_id="wt01", point_id="power", value=1.0)]
    )
    rt = _runtime("iec104_scada", old_sink)
    await rt.start()
    try:
        assert await _interrogate_values(port) == {1001: 1.0}

        new_sink = IEC104Sink(cfg)
        await new_sink.write(
            [PointValue(device_id="wt01", point_id="power", value=2.0)]
        )
        await rt.sink_runtime.rebuild_sink("iec104_scada", cfg, new_sink)

        # 同端口重连后必须读到新 sink 链路（exporter→c104 从站点→wire）的值。
        assert await _interrogate_values(port) == {1001: 2.0}
        assert rt.sinks["iec104_scada"] is new_sink
        assert new_sink.health().healthy is True
    finally:
        await rt.stop()


async def test_iec104_failed_rebuild_restores_old_listener() -> None:
    old_port = free_port()
    blocked_port = free_port()
    old_cfg = _iec104_config(old_port)
    old_sink = IEC104Sink(old_cfg)
    await old_sink.write(
        [PointValue(device_id="wt01", point_id="power", value=1.0)]
    )
    rt = _runtime("iec104_scada", old_sink)
    await rt.start()

    blocker = await asyncio.start_server(
        lambda _reader, writer: writer.close(),
        "127.0.0.1",
        blocked_port,
    )
    try:
        new_cfg = _iec104_config(blocked_port)
        new_sink = IEC104Sink(new_cfg)

        # IEC104SlaveServer 把 bind 失败归一为 OSError（Modbus 侧 pymodbus
        # 包装为 RuntimeError）；rebuild_sink 原样重抛并恢复旧实例。
        with pytest.raises(OSError):
            await rt.sink_runtime.rebuild_sink("iec104_scada", new_cfg, new_sink)

        assert rt.sinks["iec104_scada"] is old_sink
        assert old_sink.health().healthy is True

        # 回滚后旧 sink 必须在协议层继续工作，而不只是端口可 connect。
        assert await _interrogate_values(old_port) == {1001: 1.0}
    finally:
        blocker.close()
        await blocker.wait_closed()
        await rt.stop()


def test_listener_ports_are_available_before_component_run() -> None:
    """Sanity check：free_port 返回的端口当前可绑定。"""
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


async def test_modbus_failed_rebuild_restores_old_listener() -> None:
    old_port = free_port()
    blocked_port = free_port()
    old_cfg = _modbus_config(old_port)
    old_sink = ModbusSink(old_cfg)
    await old_sink.write(
        [PointValue(device_id="wt01", point_id="power", value=1.0)]
    )
    rt = _runtime("modbus_scada", old_sink)
    await rt.start()

    blocker = await asyncio.start_server(
        lambda _reader, writer: writer.close(),
        "127.0.0.1",
        blocked_port,
    )
    try:
        new_cfg = _modbus_config(blocked_port)
        new_sink = ModbusSink(new_cfg)

        with pytest.raises(RuntimeError):
            await rt.sink_runtime.rebuild_sink("modbus_scada", new_cfg, new_sink)

        assert rt.sinks["modbus_scada"] is old_sink
        assert old_sink.health().healthy is True

        client = AsyncModbusTcpClient("127.0.0.1", port=old_port)
        assert await client.connect()
        try:
            response = await client.read_holding_registers(
                100, count=2, device_id=1
            )
            assert not response.isError()
            assert list(response.registers) == [0x3F80, 0x0000]
        finally:
            client.close()
    finally:
        blocker.close()
        await blocker.wait_closed()
        await rt.stop()
