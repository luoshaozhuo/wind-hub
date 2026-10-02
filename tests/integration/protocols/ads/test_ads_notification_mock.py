"""Integration test — ADS device-notification subscribe against a mocked pyads connection.

Drives the driver end-to-end: connect → subscribe (enabled via config) → the
mock invokes the registered callback (simulating pyads' worker thread) → the
pushed value is delivered back through the async ``on_data`` callback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace

import pyads  # noqa: F401 — real module, only ``Connection`` is patched
import pytest

from wind_hub_core.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig
from wind_hub_core.model.point import PointRef, PointValue
from wind_hub_core.protocol.ads.driver import ADSDriver

_AMS_NET_ID = "192.168.0.100.1.1"


class MockAdsConnection:
    """Synchronous stand-in for ``pyads.Connection`` with notification support."""

    def __init__(self, ams_net_id: str | None, ams_port: int | None, ip: str | None) -> None:
        self.ams_net_id = ams_net_id
        self.ip = ip
        self.is_open = False
        self.callbacks: dict[tuple[int, int], object] = {}

    def set_timeout(self, ms: int) -> None:
        pass

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def get_symbol(self, name: str) -> object:
        return SimpleNamespace(
            index_group=0x4020,
            index_offset=100,
            plc_type=pyads.PLCTYPE_REAL,
        )

    def notification(self, plc_datatype: object) -> object:
        def decorator(callback: object) -> object:
            return callback

        return decorator

    def add_device_notification(
        self,
        address: tuple[int, int],
        attr: object,
        callback: object,
        user_handle: object = None,
    ) -> tuple[object, object]:
        self.callbacks[address] = callback
        return ((address, "handle"), user_handle)

    def del_device_notification(self, handle: object, user_handle: object) -> None:
        pass


@pytest.fixture
def ads_connection(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(pyads, "Connection", MockAdsConnection)
    monkeypatch.setattr(
        pyads, "NotificationAttrib", lambda length, cycle_time=0.02, max_delay=0.06: None
    )
    yield


def _make_device_config_enabled() -> DeviceConfig:
    return DeviceConfig(
        device_id="test-plc",
        protocol="ads",
        endpoint=Endpoint(
            host="192.168.0.100",
            port=48898,
            extensions={
                "target_net_id": _AMS_NET_ID,
                "timeout": 3.0,
                # 订阅参数不经正式 Schema——经 endpoint.extensions 透传（诊断用）
                "subscribe_enabled": True,
            },
        ),
        point_table="t1",
    )


def _make_symbol_point(point_id: str, symbol: str) -> PointConfig:
    return PointConfig(
        point_groups=["default"],
        point_id=point_id,
        address=PointAddress(symbol=symbol),
        data_type="float32",
    )


class TestAdsNotificationIntegration:
    async def test_subscribe_delivers_notification(self, ads_connection: None) -> None:
        driver = ADSDriver(_make_device_config_enabled())
        driver.set_points_mapping([_make_symbol_point("rotor.speed", "MAIN.rotorSpeed")])

        await driver.connect()
        received: list[PointValue] = []

        async def on_data(pv: PointValue) -> None:
            received.append(pv)

        try:
            await driver.subscribe(
                [PointRef(device_id="test-plc", point_id="rotor.speed")],
                on_data,
                interval=0.1,  # notification cycle_time（秒），驱动要求 > 0
            )
            # Simulate pyads' worker thread firing the callback.
            sub = next(iter(driver._subscriptions))  # noqa: SLF001
            conn = sub._connections[0]  # noqa: SLF001
            address = (0x4020, 100)
            conn.callbacks[address](None, address, None, 1500.5)
            await asyncio.sleep(0.05)  # let the loop deliver through on_data
        finally:
            await driver.close()

        assert len(received) == 1
        assert received[0].device_id == "test-plc"
        assert received[0].point_id == "rotor.speed"
        assert received[0].value == pytest.approx(1500.5)
        assert received[0].source == "ads"

    async def test_subscribe_raises_when_not_enabled(self, ads_connection: None) -> None:
        driver = ADSDriver(
            DeviceConfig(
                device_id="test-plc",
                protocol="ads",
                endpoint=Endpoint(
                    host="192.168.0.100",
                    port=48898,
                    extensions={"target_net_id": _AMS_NET_ID},
                ),
                point_table="t1",
            )
        )
        await driver.connect()
        try:
            with pytest.raises(NotImplementedError):
                await driver.subscribe([], lambda pv: _noop())
        finally:
            await driver.close()


async def _noop() -> None:
    return None
