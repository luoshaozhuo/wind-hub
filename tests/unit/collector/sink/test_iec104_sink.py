"""新 Collector IEC104Sink 单元测试。

不建立外部主站连接：直接验证 write → exporter → c104 从站点的映射结果
（值、scale/offset、未映射忽略）与 open/close 生命周期委托。
"""

from __future__ import annotations

import pytest

from collector.application.sinks import (
    IEC104SinkConnection,
    ResolvedSinkConfig,
)
from collector.domain.point_value import PointValue
from collector.infrastructure.sink.iec104 import IEC104Sink
from core.application import ConfigError

c104 = pytest.importorskip("c104")


def _config(port: int = 12404) -> ResolvedSinkConfig:
    return ResolvedSinkConfig(
        name="scada",
        type="iec104",
        connection=IEC104SinkConnection(
            host="127.0.0.1",
            port=port,
            common_address=1,
        ),
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


def _point_value(sink: IEC104Sink, ioa: int) -> object:
    """读取底层 c104 从站点当前值（单元测试专用观测口）。"""
    point = sink._server._station.get_point(ioa)  # noqa: SLF001
    assert point is not None
    return point.value


async def test_write_exports_and_updates_point() -> None:
    sink = IEC104Sink(_config())
    await sink.write([PointValue(device_id="wt01", point_id="wind_speed", value=3.0)])
    # scale=2.0, offset=1.0 → 3.0*2+1 = 7.0
    assert _point_value(sink, 1001) == pytest.approx(7.0)  # type: ignore[operator]


async def test_write_ignores_unmapped_point() -> None:
    sink = IEC104Sink(_config())
    await sink.write([PointValue(device_id="wt01", point_id="other", value=3.0)])
    assert sink._server._station.get_point(1001) is None  # noqa: SLF001


async def test_open_close_lifecycle() -> None:
    sink = IEC104Sink(_config(port=12405))
    assert sink.exclusive_open is True
    assert sink.health().healthy is False
    await sink.open()
    assert sink.health().healthy is True
    assert sink.port == 12405
    await sink.close()
    assert sink.health().healthy is False
    await sink.close()  # 幂等


def test_invalid_connection_rejected() -> None:
    """错误 connection 类型在构造期即报 ConfigError。"""
    from collector.application.sinks import FileSinkConnection

    with pytest.raises(ConfigError, match="IEC104SinkConnection"):
        IEC104Sink(
            ResolvedSinkConfig.model_construct(
                name="scada",
                type="iec104",
                connection=FileSinkConnection(path="/tmp/x.jsonl"),
                points=[],
            )
        )
