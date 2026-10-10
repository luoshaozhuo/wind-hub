"""IEC104 Sink TCP component test（c104 从站 × c104 测试主站）。

单元测试（tests/unit/collector/sink/test_iec104_sink.py）不建立外部主站
连接；本文件保留真实 wire 覆盖：真实 IEC104Sink 从站 × 独立 c104 主站
（tests/support/iec104_master.py）总召回收值，经真实 TCP/ASDU 往返验证。
"""

from __future__ import annotations

import pytest

from collector.domain.point_value import PointValue
from collector.infrastructure.sink.iec104 import IEC104Sink
from core.application.sink_config import ResolvedSinkConfig
from tests.support.iec104_master import IEC104MasterClient
from tests.support.process import free_port

c104 = pytest.importorskip("c104")


def _config(port: int) -> ResolvedSinkConfig:
    points = []
    specs = [
        ("rotor.speed", 101, "M_ME_NC_1"),
        ("gen.power", 102, "M_ME_NC_1"),
        ("wind.speed", 103, "M_ME_NC_1"),
        ("status.running", 201, "M_SP_NA_1"),
    ]
    for point_id, ioa, type_id in specs:
        points.append(
            {
                "source": {"device_id": "wtg-001", "point_id": point_id},
                "ref": f"wtg-001.{point_id}",
                "source_data_type": "float32",
                "source_unit": "none",
                "datatype": "float32",
                "unit": "none",
                "address": {"ioa": ioa, "type_id": type_id},
            }
        )
    return ResolvedSinkConfig(
        name="scada",
        type="iec104",
        connection={
            "host": "127.0.0.1",
            "port": port,
            "common_address": 1,
        },
        points=points,
    )


@pytest.fixture
async def sink():
    port = free_port()
    iec104 = IEC104Sink(_config(port))
    await iec104.write(
        [
            PointValue(device_id="wtg-001", point_id="rotor.speed", value=1500.5),
            PointValue(device_id="wtg-001", point_id="gen.power", value=800.0),
            PointValue(device_id="wtg-001", point_id="wind.speed", value=12.5),
            PointValue(device_id="wtg-001", point_id="status.running", value=True),
        ]
    )
    await iec104.open()
    try:
        yield iec104
    finally:
        await iec104.close()


async def test_full_sink_interrogation(sink) -> None:  # type: ignore[no-untyped-def]
    """总召收回全部已写入点；值与类型经真实 c104 wire 往返验证。"""
    client = IEC104MasterClient(sink.port)
    await client.connect()
    try:
        values = await client.interrogate(expected=4)
        assert values[101] == pytest.approx(1500.5)
        assert values[102] == pytest.approx(800.0)
        assert values[103] == pytest.approx(12.5)
        assert values[201] is True
    finally:
        await client.close()
