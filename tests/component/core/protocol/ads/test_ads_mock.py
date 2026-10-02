"""Component test — ADS driver against a mocked pyads connection.

No real TwinCAT/ADS server is available in CI, so the mock below emulates a
small PLC symbol table at the ``pyads.Connection`` boundary.  The driver is
exercised end-to-end (connect → read/write → close), with the mock raising
``pyads.ADSError`` for unknown symbols to exercise the error-wrapping path.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyads  # noqa: F401 — real module, only ``Connection`` is patched
import pytest

from wind_hub_core.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub_core.model.command import Command
from wind_hub_core.model.errors import ProtocolError
from wind_hub_core.model.point import PointRef, Quality
from wind_hub_core.protocol.ads.driver import ADSDriver

# monkeypatch/fake 替代 pyads 外部组件——显式标注，不计入 real-service 验收。
pytestmark = pytest.mark.mock_service

_AMS_NET_ID = "192.168.0.100.1.1"


class MockAdsConnection:
    """Synchronous stand-in for ``pyads.Connection`` with a symbol table."""

    def __init__(self, ams_net_id: str | None, ams_port: int | None, ip: str | None) -> None:
        self.ams_net_id = ams_net_id
        self.ip = ip
        self.is_open = False
        self.symbols: dict[tuple[int, int], object] = {
            (0x4020, 0x0): 1500.5,  # rotor speed (REAL)
            (0x4020, 0x4): True,  # running flag (BOOL)
        }

    def set_timeout(self, ms: int) -> None:
        pass

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read(self, index_group: int, index_offset: int, plc_datatype: object) -> object:
        if not self.is_open:
            raise pyads.ADSError(text="connection closed")
        if (index_group, index_offset) not in self.symbols:
            raise pyads.ADSError(text="symbol not found")
        return self.symbols[(index_group, index_offset)]

    def write(
        self, index_group: int, index_offset: int, value: object, plc_datatype: object
    ) -> None:
        if not self.is_open:
            raise pyads.ADSError(text="connection closed")
        self.symbols[(index_group, index_offset)] = value


@pytest.fixture
def ads_connection(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(pyads, "Connection", MockAdsConnection)
    yield


def _make_device_config() -> DeviceConfig:
    return DeviceConfig(
        device_id="test-plc",
        protocol="ads",
        endpoint=Endpoint(
            host="192.168.0.100",
            port=48898,
            extensions={"target_net_id": _AMS_NET_ID, "timeout": 3.0},
        ),
        point_table="t1",
        read_mode="sequential",
    )


def _make_point_config(
    point_id: str,
    data_type: str,
    index_offset: int,
) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(index_group=0x4020, index_offset=index_offset),
        data_type=data_type,
    )


class TestAdsIntegration:
    async def test_connect_read_multiple_types(self, ads_connection: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_point_config("rotor.speed", "float32", 0x0),
                _make_point_config("running", "bool", 0x4),
            ]
        )

        await driver.connect()
        try:
            values = await driver.read(
                [
                    PointRef(device_id="test-plc", point_id="rotor.speed"),
                    PointRef(device_id="test-plc", point_id="running"),
                ]
            )
        finally:
            await driver.close()

        assert values[0].value == pytest.approx(1500.5)
        assert values[0].quality == Quality.GOOD
        assert values[0].source == "ads"
        assert values[1].value is True

    async def test_write_round_trip(self, ads_connection: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping([_make_point_config("setpoint", "float32", 0x8)])

        await driver.connect()
        try:
            results = await driver.write(
                [Command(command_id="c1", device_id="test-plc", point_id="setpoint", value=99.5)]
            )
            values = await driver.read([PointRef(device_id="test-plc", point_id="setpoint")])
        finally:
            await driver.close()

        assert results[0].success is True
        assert values[0].value == pytest.approx(99.5)

    async def test_read_unknown_symbol_wraps_error(self, ads_connection: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping([_make_point_config("missing", "float32", 0xFFFF)])

        await driver.connect()
        try:
            with pytest.raises(ProtocolError):
                await driver.read([PointRef(device_id="test-plc", point_id="missing")])
        finally:
            await driver.close()
