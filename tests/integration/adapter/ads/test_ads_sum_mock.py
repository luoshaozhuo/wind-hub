"""Integration test — ADS Sum read against a mocked pyads connection.

Exercises the driver end-to-end (connect → Sum read → close) using symbol
addressing.  The mock returns a partial symbol dict: symbols the "PLC" does not
know are simply omitted, which the driver reports as ``BAD`` — modelling a
per-sub-command failure without a real TwinCAT runtime.
"""

from __future__ import annotations

from collections.abc import Iterator

import pyads  # noqa: F401 — real module, only ``Connection`` is patched
import pytest

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.point import PointRef, Quality

_AMS_NET_ID = "192.168.0.100.1.1"


class MockAdsConnection:
    """Synchronous stand-in for ``pyads.Connection`` with a symbol table."""

    def __init__(self, ams_net_id: str | None, ams_port: int | None, ip: str | None) -> None:
        self.ams_net_id = ams_net_id
        self.ip = ip
        self.is_open = False
        self.symbol_values: dict[str, object] = {
            "MAIN.rotorSpeed": 1500.5,
            "MAIN.genPower": 800.0,
        }

    def set_timeout(self, ms: int) -> None:
        pass

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read_list_by_name(
        self, names: list[str], *args: object, **kwargs: object
    ) -> dict[str, object]:
        if not self.is_open:
            raise pyads.ADSError(text="connection closed")
        return {n: self.symbol_values[n] for n in names if n in self.symbol_values}


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
        read_mode="sum",
    )


def _make_symbol_point(point_id: str, symbol: str) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(symbol=symbol),
        data_type="float32",
    )


class TestAdsSumIntegration:
    async def test_sum_read_happy_path(self, ads_connection: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("rotor.speed", "MAIN.rotorSpeed"),
                _make_symbol_point("gen.power", "MAIN.genPower"),
            ]
        )

        await driver.connect()
        try:
            values = await driver.read(
                [
                    PointRef(device_id="test-plc", point_id="rotor.speed"),
                    PointRef(device_id="test-plc", point_id="gen.power"),
                ]
            )
        finally:
            await driver.close()

        assert values[0].value == pytest.approx(1500.5)
        assert values[0].quality == Quality.GOOD
        assert values[1].value == pytest.approx(800.0)

    async def test_sum_read_missing_symbol_reported_bad(self, ads_connection: None) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("rotor.speed", "MAIN.rotorSpeed"),
                _make_symbol_point("missing", "MAIN.doesNotExist"),
            ]
        )

        await driver.connect()
        try:
            values = await driver.read(
                [
                    PointRef(device_id="test-plc", point_id="rotor.speed"),
                    PointRef(device_id="test-plc", point_id="missing"),
                ]
            )
        finally:
            await driver.close()

        assert values[0].quality == Quality.GOOD
        assert values[1].quality == Quality.BAD
        assert values[1].value is None
