"""ADSDriver × 真实 AMS/TCP 协议栈集成（pyads AdsTestServer 或真实 PLC）。

与 commander 级 ``test_commander_ads_real.py`` 互补：本文件直接驱动新的
ADSDriver/_AdsSession/ADSSubscription，验证 fake 替身无法证明的部分——
真实 pyads 线格式、Sum Read/Write、Notification 推送与 symbol 解析。
未配置真实 PLC 时默认使用进程内 AdsTestServer（``real_service``）。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pyads  # type: ignore[import-untyped]
import pytest

from core.application.protocol_contract import (
    ProtocolSample,
    ProtocolWrite,
    Quality,
    WriteOutcome,
)
from core.application.settings import ADSLocalConfig
from core.domain import (
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.env import ads_config_from_env
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.real_service, pytest.mark.ads]
if ads_config_from_env() is not None:
    pytestmark.append(pytest.mark.hardware)  # type: ignore[attr-defined]


def _point(point_id: str, symbol: str, data_type: str = "REAL") -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"symbol": symbol, "data_type": data_type},
    )


def _driver(ads: dict) -> ADSDriver:
    points = PointTable(
        PointTableId("ads_real"),
        Protocol("ads"),
        {
            "readback": _point("readback", ads["read_symbol"]),
            "setpoint": _point("setpoint", ads["write_symbol"]),
        },
    )
    return ADSDriver(
        ConnectionEndpoint(ads["host"], 851),
        points,
        {"target_net_id": ads["net_id"], "timeout": 5.0, "route_auto_add": False},
    )


@pytest.fixture
async def ads_driver(ads_service: dict) -> AsyncIterator[ADSDriver]:
    await ADSDriver.initialize_local(
        ADSLocalConfig(
            local_ams_net_id=ads_service["local_net_id"],
            local_ip=ads_service["local_ip"],
        )
    )
    driver = _driver(ads_service)
    try:
        yield driver
    finally:
        if driver.is_open():
            await driver.close()
        await ADSDriver.close_local()


class TestRealDriverIO:
    async def test_connect_health_and_symbol_resolution(self, ads_driver: ADSDriver) -> None:
        await ads_driver.connect()
        assert ads_driver.is_open()
        health = await ads_driver.health()
        assert health.healthy, health.detail
        read_point = ads_driver._points["readback"]
        assert read_point.address_resolved

    async def test_sum_read_returns_real_value(self, ads_driver: ADSDriver) -> None:
        await ads_driver.connect()
        sample = await ads_driver.read_one("readback")
        assert sample.quality is Quality.GOOD
        assert sample.value == pytest.approx(21.5, abs=1e-6)

    async def test_sum_write_then_read_back(self, ads_driver: ADSDriver) -> None:
        await ads_driver.connect()
        result = await ads_driver.write_one(ProtocolWrite("setpoint", 17.25))
        assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS, result.message
        sample = await ads_driver.read_one("setpoint")
        assert sample.quality is Quality.GOOD
        assert sample.value == pytest.approx(17.25, abs=1e-6)

    async def test_unknown_symbol_fails_connect_and_releases_identity(
        self, ads_service: dict
    ) -> None:
        if ads_service["backend"] != "testserver":
            pytest.skip("symbol 集合固定为 testserver fixture")
        await ADSDriver.initialize_local(
            ADSLocalConfig(
                local_ams_net_id=ads_service["local_net_id"],
                local_ip=ads_service["local_ip"],
            )
        )
        try:
            table = PointTable(
                PointTableId("ads_real"),
                Protocol("ads"),
                {"ghost": _point("ghost", "Main.does_not_exist")},
            )
            driver = ADSDriver(
                ConnectionEndpoint(ads_service["host"], 851),
                table,
                {"target_net_id": ads_service["net_id"]},
            )
            # 1861 = GLOBAL_RESOURCE_NOT_FOUND / symbol not found（pyads ADSError）。
            with pytest.raises(pyads.ADSError, match="1861"):
                await driver.connect()
            assert not driver.is_open()
        finally:
            await ADSDriver.close_local()


class TestRealNotifications:
    async def test_device_cycle_notification_delivered_and_stops(
        self,
        ads_driver: ADSDriver,
        ads_service: dict,
    ) -> None:
        if ads_service["backend"] != "testserver":
            pytest.skip("需要直接拿到服务端 PLCVariable 注入变化")
        await ads_driver.connect()
        received: list[ProtocolSample] = []
        gate = asyncio.Event()

        async def callback(batch: tuple[ProtocolSample, ...]) -> None:
            received.extend(batch)
            gate.set()

        sub = ads_driver.subscribe(("readback",), callback, interval=0.01)
        await sub.start()
        variable = ads_service["variables"][ads_service["read_symbol"]]
        variable.write(struct.pack("<f", 42.5))
        await asyncio.wait_for(gate.wait(), timeout=5)
        matching = [sample for sample in received if sample.point_id == "readback"]
        assert matching[-1].value == pytest.approx(42.5, abs=1e-6)
        assert matching[-1].timestamp_source == "device"
        await sub.stop()
        count_after_stop = len(received)
        variable.write(struct.pack("<f", 43.5))
        await asyncio.sleep(0.3)
        assert len(received) == count_after_stop

    async def test_driver_close_releases_notification(
        self,
        ads_driver: ADSDriver,
        ads_service: dict,
    ) -> None:
        if ads_service["backend"] != "testserver":
            pytest.skip("需要直接拿到服务端 PLCVariable 注入变化")
        await ads_driver.connect()

        async def callback(batch: tuple[ProtocolSample, ...]) -> None:
            return None

        sub = ads_driver.subscribe(("readback",), callback, interval=0.01)
        await sub.start()
        variable = ads_service["variables"][ads_service["read_symbol"]]
        await ads_driver.close()
        registrations = variable.notifications
        assert registrations == []
