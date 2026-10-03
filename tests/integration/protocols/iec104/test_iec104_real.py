"""IEC104 驱动 × 真实 IEC104 server fixture 集成测试。

被测组件是 :class:`IEC104Driver`（TCP session、STARTDT 握手、总召、
ASDU 解码、最新值缓存）；对端是真实 IEC104 从站 fixture
（预设 ``{100: 1500.5, 200: 50.0}``，M_ME_NC_1 float32）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.support.config_helper import write_config_tree
from tests.support.process import free_port
from tests.support.wait import wait_until
from wind_hub_core.config.loader import load_config
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointRef, Quality
from wind_hub_core.protocol.iec104.driver import IEC104Driver

pytestmark = pytest.mark.real_service

IEC104_POINTS: list[dict[str, Any]] = [
    {
        "point_id": "meas.active_power",
        "point_groups": ["telemetry"],
        "address": {"ioa": 100},
        "data_type": "float32",
    },
    {
        "point_id": "meas.frequency",
        "point_groups": ["telemetry"],
        "address": {"ioa": 200},
        "data_type": "float32",
    },
]


def _ref(point_id: str) -> PointRef:
    return PointRef(device_id="iec104-1", point_id=point_id)


@pytest.fixture
async def iec104_driver(tmp_path: Path) -> AsyncIterator[tuple[IEC104Driver, IEC104MockServer]]:
    port = free_port()
    server = IEC104MockServer(port=port)
    config_dir = write_config_tree(
        tmp_path / "cfg",
        devices=[
            {
                "device_id": "iec104-1",
                "protocol": "iec104",
                "point_table": "iec104",
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": port,
                    "extensions": {
                        "common_addr": 1,
                        # 缩短协议定时器，避免测试拖长。
                        "t0": 5.0,
                        "t1": 3.0,
                        "t2": 2.0,
                        "t3": 5.0,
                    },
                },
            }
        ],
        point_tables={"iec104": {"points": list(IEC104_POINTS)}},
        sinks=[{"name": "null_sink", "type": "null"}],
        tasks=[],
    )
    config = load_config(config_dir)
    device_cfg = config.devices.devices[0]
    points = list(config.point_tables.tables["iec104"].points)
    await server.start()
    driver = IEC104Driver(device_cfg)
    driver.set_points_mapping(points)
    try:
        yield driver, server
    finally:
        await driver.close()
        await server.stop()


async def _read_good(driver: IEC104Driver, point_id: str) -> Any:
    """轮询读直到指定点拿到 GOOD 缓存值。"""
    async def _probe() -> Any:
        try:
            values = await driver.read([_ref(point_id)])
        except ProtocolError:
            return None
        value = values[0]
        return value.value if value.quality is Quality.GOOD else None

    return await wait_until(
        _probe, timeout=10.0, description=f"iec104 cache filled for {point_id}"
    )


class TestConnectAndInterrogate:
    async def test_connect_reports_healthy(self, iec104_driver) -> None:
        driver, _ = iec104_driver
        await driver.connect()
        assert driver.health().healthy is True

    async def test_general_interrogation_fills_cache(self, iec104_driver) -> None:
        driver, _ = iec104_driver
        await driver.connect()
        await driver.interrogate()
        assert await _read_good(driver, "meas.active_power") == pytest.approx(1500.5)
        assert await _read_good(driver, "meas.frequency") == pytest.approx(50.0)

    async def test_read_unknown_point_returns_bad_quality(self, iec104_driver) -> None:
        driver, _ = iec104_driver
        await driver.connect()
        values = await driver.read([_ref("ghost.point")])
        assert values[0].value is None
        assert values[0].quality is Quality.BAD

    async def test_read_without_connect_raises(self, iec104_driver) -> None:
        driver, _ = iec104_driver
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([_ref("meas.active_power")])


class TestSubscription:
    async def test_subscription_receives_interrogation_data(self, iec104_driver) -> None:
        driver, _ = iec104_driver
        received: list = []

        async def _callback(value) -> None:
            received.append(value)

        await driver.connect()
        handle = await driver.subscribe([_ref("meas.active_power")], _callback)
        await driver.interrogate()

        await wait_until(
            lambda: received or None,
            timeout=10.0,
            description="subscription callback receives interrogation data",
        )
        assert received[0].point_id == "meas.active_power"
        assert received[0].value == pytest.approx(1500.5)
        await handle.close()


class TestClose:
    async def test_close_disconnects_and_rejects_read(self, iec104_driver) -> None:
        driver, _ = iec104_driver
        await driver.connect()
        await driver.close()
        assert driver.health().healthy is False
        with pytest.raises(ProtocolError, match="not connected"):
            await driver.read([_ref("meas.active_power")])
