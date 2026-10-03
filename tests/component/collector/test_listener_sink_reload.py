"""监听型 Sink 真实端口热重载 component test。"""

from __future__ import annotations

import asyncio
import socket

import pytest
from pymodbus.client import AsyncModbusTcpClient

from tests.support.process import free_port
from wind_hub_collector.adapter.outbound.sink.iec104 import IEC104Sink
from wind_hub_collector.adapter.outbound.sink.modbus import ModbusSink
from wind_hub_collector.application.runtime import Runtime
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config.schema import RuntimeConfig
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.point import PointValue

pytestmark = pytest.mark.real_service


def _runtime(sink_name: str, sink) -> Runtime:  # type: ignore[no-untyped-def]
    return Runtime(
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
            "batch_size": 50,
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
        await rt.rebuild_sink("modbus_scada", cfg, new_sink)

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


async def test_iec104_runtime_rebuild_reuses_same_listener_port() -> None:
    port = free_port()
    cfg = _iec104_config(port)
    old_sink = IEC104Sink(cfg)
    rt = _runtime("iec104_scada", old_sink)
    await rt.start()
    try:
        new_sink = IEC104Sink(cfg)
        await rt.rebuild_sink("iec104_scada", cfg, new_sink)

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        del reader
        writer.close()
        await writer.wait_closed()
        assert rt.sinks["iec104_scada"] is new_sink
        assert new_sink.health().healthy is True
    finally:
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
            await rt.rebuild_sink("modbus_scada", new_cfg, new_sink)

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
