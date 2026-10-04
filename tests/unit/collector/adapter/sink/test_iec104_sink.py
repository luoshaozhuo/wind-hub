"""IEC104Sink 单元测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.adapter.outbound.sink.iec104 import IEC104Sink
from wind_hub_core.config.sinks import ResolvedSinkConfig
from wind_hub_core.model.point import PointValue

pytestmark = pytest.mark.asyncio


def _config() -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name="scada",
        type="iec104",
        connection={
            "host": "127.0.0.1",
            "port": 12404,
            "common_address": 1,
            "batch_size": 50,
        },
        points=[
            {
                "source": {"device_id": "wt01", "point_id": "wind_speed"},
                "ref": "wt01.wind_speed",
                "source_data_type": "float32",
                "source_unit": "meter_per_second",
                "datatype": "float32",
                "unit": "meter_per_second",
                "scale": 2.0,
                "offset": 1.0,
                "address": {"ioa": 1001, "type_id": "M_ME_NC_1"},
            }
        ],
    )


async def test_write_exports_and_updates_snapshot() -> None:
    sink = IEC104Sink(_config())
    await sink.write(
        [PointValue(device_id="wt01", point_id="wind_speed", value=3.0)]
    )
    value = sink.snapshot.get(1001)
    assert value is not None
    assert value.ref == "wt01.wind_speed"
    assert value.value == 7.0


async def test_write_ignores_unmapped_point() -> None:
    sink = IEC104Sink(_config())
    await sink.write([PointValue(device_id="wt01", point_id="other", value=3.0)])
    assert sink.snapshot.size == 0


async def test_open_close_delegate_to_server() -> None:
    sink = IEC104Sink(_config())
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
