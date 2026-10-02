"""ADS 地址解析 + Sum Read 的 mock 集成测试。"""

from __future__ import annotations

import ctypes
import struct
from collections.abc import Iterator
from types import SimpleNamespace

import pyads
import pytest

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub.domain.model.point import PointRef, Quality

_AMS_NET_ID = "192.168.0.100.1.1"


class MockAdsConnection:
    """提供 PLC symbol 地址事实的最小 pyads.Connection 替身。"""

    def __init__(self, ams_net_id: str | None, ams_port: int | None, ip: str | None) -> None:
        self.ams_net_id = ams_net_id
        self.ams_port = ams_port
        self.ip = ip
        self.is_open = False
        self._port = 1
        self._adr = object()
        self.symbols = {
            "MAIN.rotorSpeed": (0x4020, 100),
            "MAIN.genPower": (0x4020, 104),
        }

    def set_timeout(self, ms: int) -> None:
        del ms

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def get_symbol(self, symbol: str) -> object:
        if symbol not in self.symbols:
            raise pyads.ADSError(1808, "symbol not found")
        index_group, index_offset = self.symbols[symbol]
        return SimpleNamespace(
            index_group=index_group,
            index_offset=index_offset,
            plc_type=ctypes.c_float,
            symbol_type="REAL",
        )


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


def _payload(values: list[float], errors: list[int] | None = None) -> bytes:
    errors = errors or [0] * len(values)
    return b"".join(struct.pack("<I", error) for error in errors) + b"".join(
        struct.pack("<f", value) for value in values
    )


class TestAdsSumIntegration:
    async def test_sum_read_happy_path(
        self, ads_connection: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("rotor.speed", "MAIN.rotorSpeed"),
                _make_symbol_point("gen.power", "MAIN.genPower"),
            ]
        )
        await driver.connect()
        monkeypatch.setattr(driver, "_sum_read_bytes", lambda addresses: _payload([1500.5, 800.0]))
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

    async def test_sum_read_missing_symbol_reported_bad(
        self, ads_connection: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        driver = ADSDriver(_make_device_config())
        driver.set_points_mapping(
            [
                _make_symbol_point("rotor.speed", "MAIN.rotorSpeed"),
                _make_symbol_point("missing", "MAIN.doesNotExist"),
            ]
        )
        await driver.connect()
        monkeypatch.setattr(driver, "_sum_read_bytes", lambda addresses: _payload([1500.5]))
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
