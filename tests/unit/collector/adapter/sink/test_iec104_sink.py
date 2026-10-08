"""IEC104Sink 单元测试。

不建立网络连接：直接验证 write → exporter → c104 从站点的映射结果
（值、scale/offset、type_id 冲突）与 open/close 生命周期委托。
"""

import pytest

from wind_hub_collector.adapter.outbound.sink.iec104 import IEC104Sink
from wind_hub_core.config import ResolvedSinkConfig
from wind_hub_core.model.errors import ConfigError
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


def _point_value(sink: IEC104Sink, ioa: int):  # type: ignore[no-untyped-def]
    """读取底层 c104 从站点当前值（单元测试专用观测口）。"""
    point = sink._server._station.get_point(ioa)  # noqa: SLF001
    assert point is not None
    return point.value


async def test_write_exports_and_updates_point() -> None:
    sink = IEC104Sink(_config())
    await sink.write([PointValue(device_id="wt01", point_id="wind_speed", value=3.0)])
    # scale=2.0, offset=1.0 → 3.0*2+1 = 7.0
    assert _point_value(sink, 1001) == pytest.approx(7.0)


async def test_write_ignores_unmapped_point() -> None:
    sink = IEC104Sink(_config())
    await sink.write([PointValue(device_id="wt01", point_id="other", value=3.0)])
    assert sink._server._station.get_point(1001) is None  # noqa: SLF001


async def test_open_close_lifecycle() -> None:
    sink = IEC104Sink(_config())
    assert sink.health().healthy is False
    await sink.open()
    assert sink.health().healthy is True
    assert sink.port == 12404
    await sink.close()
    assert sink.health().healthy is False
    # close 幂等
    await sink.close()


async def test_invalid_connection_rejected() -> None:
    """非法 connection（port 越界）在配置解析阶段即报 ConfigError。"""
    with pytest.raises(ConfigError):
        IEC104Sink(
            ResolvedSinkConfig(
                name="bad",
                type="iec104",
                connection={"host": "127.0.0.1", "port": 0},
                points=[],
            )
        )
