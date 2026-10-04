"""ModbusSink 单元测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.adapter.outbound.sink.modbus import ModbusSink
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.point import PointValue

pytestmark = pytest.mark.asyncio


def _config() -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name="modbus_scada",
        type="modbus",
        connection={"host": "127.0.0.1", "port": 1502},
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


async def test_write_updates_store() -> None:
    sink = ModbusSink(_config())
    await sink.write(
        [PointValue(device_id="wt01", point_id="power", value=1.0)]
    )
    assert sink.data_path.store.read_registers(1, "holding", 100, 2) == [
        0x3F80,
        0x0000,
    ]


async def test_open_close_delegate_to_server() -> None:
    sink = ModbusSink(_config())
    server = MagicMock()
    server.start = AsyncMock()
    server.stop = AsyncMock()
    server.health = MagicMock()
    sink._server = server  # noqa: SLF001

    await sink.open()
    await sink.flush()
    await sink.close()

    server.start.assert_awaited_once()
    server.stop.assert_awaited_once()


def test_listener_requires_exclusive_reload() -> None:
    sink = ModbusSink(_config())
    assert sink.exclusive_open is True
