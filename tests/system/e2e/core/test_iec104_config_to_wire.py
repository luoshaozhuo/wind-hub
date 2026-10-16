"""Core-only E2E：YAML 配置目录 → 领域快照 → 驱动 → 真实 c104 wire。

链路：``YamlConfigAdapter.load()`` 加载
``tests/fixtures/configs/example_iec104``，由快照中的
device/model/point table/protocol options 经 ``ProtocolRegistry``
创建 ``IEC104Driver``，对端为真实 c104 Server（IEC104ControlServer）。

覆盖：建连与健康、显式总召经订阅分发、spontaneous 主动上送、
值/timestamp/quality 语义、C_SC_NA_1 遥控到达对端、关闭与资源释放。
不经过 collector/commander/server 任何进程层。
"""

from __future__ import annotations

import asyncio

import c104
import pytest

from core.application import ProtocolRegistry, ProtocolSample, ProtocolWrite, Quality
from core.application.protocol_contract import WriteOutcome
from core.domain import ConnectionEndpoint
from core.infrastructure.config import YamlConfigAdapter
from core.infrastructure.protocol.iec104 import IEC104Driver
from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.support.wait import wait_until

pytestmark = pytest.mark.real_service

_CONFIG_DIR = "tests/fixtures/configs/example_iec104"

_DATA_POINTS = {100: 1500.5, 200: 50.0}


def _load_driver(port: int) -> IEC104Driver:
    snapshot = YamlConfigAdapter(_CONFIG_DIR).load()
    device = next(iter(snapshot.site.devices.values()))
    model = snapshot.device_models[device.device_model_id]
    table = snapshot.point_tables[model.point_table_id]
    options = dict(snapshot.protocol_options_by_device[device.device_id])
    assert {"meas_power", "meas_freq", "cmd_breaker"} <= set(table.points)

    registry = ProtocolRegistry()
    registry.register("iec104", IEC104Driver)
    driver = registry.create(
        ConnectionEndpoint("127.0.0.1", port),
        table,
        options,
    )
    assert isinstance(driver, IEC104Driver)
    return driver


class TestIEC104ConfigToWire:
    @pytest.mark.asyncio
    async def test_yaml_config_drives_real_iec104_session(self) -> None:
        server = IEC104ControlServer(
            data_points=_DATA_POINTS,
            command_points={300: c104.Type.C_SC_NA_1},
        )
        await server.start()
        samples: list[ProtocolSample] = []
        received = asyncio.Event()

        async def on_sample(sample: ProtocolSample) -> None:
            samples.append(sample)
            received.set()

        driver = _load_driver(server.port)
        try:
            await driver.connect()
            assert driver.is_open()
            health = await driver.health()
            assert health.healthy

            subscription = await driver.subscribe((), on_sample)

            # 显式总召：两个监测点经订阅回调到达，语义完整
            await driver.interrogate()
            by_point = {sample.point_id: sample for sample in samples}
            assert by_point["meas_power"].value == pytest.approx(1500.5)
            assert by_point["meas_freq"].value == pytest.approx(50.0)
            for sample in by_point.values():
                assert sample.quality is Quality.GOOD
                assert sample.timestamp.tzinfo is not None

            # 主动上报：对端改值后新样本到达
            received.clear()
            station = server._station
            assert station is not None
            station_point = station.get_point(100)
            assert station_point is not None
            station_point.value = 1600.25
            assert station_point.transmit(c104.Cot.SPONTANEOUS)
            await asyncio.wait_for(received.wait(), timeout=5.0)

            def _spontaneous_arrived() -> bool:
                return any(
                    s.point_id == "meas_power" and s.value == pytest.approx(1600.25)
                    for s in samples
                )

            await wait_until(_spontaneous_arrived, timeout=5.0)

            # 遥控命令到达对端
            result = await driver.write_one(ProtocolWrite(point="cmd_breaker", value=True))
            assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS, result.message
            assert server.last_control.get("C_SC_NA_1") == 300

            await subscription.close()
        finally:
            await driver.close()
            await server.stop()

        assert not driver.is_open()
        health = await driver.health()
        assert not health.healthy
