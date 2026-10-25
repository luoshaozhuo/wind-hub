"""ADS 关闭竞争：通知洪流中 close、start 与 close 竞争、并发关闭。

全部跑在真实 AMS/TCP 栈（pyads AdsTestServer，``real_service``）。
旧用例中的链路 drop 变体在 test_ads_reliability.py F07 覆盖。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import threading
import time
from typing import Any

import pytest

from core.application.protocol_contract import ProtocolSample
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
from core.infrastructure.protocol.ads.driver import _TARGET_OWNERS, ADSState
from tests.support import ads_testserver
from tests.support.env import ads_config_from_env
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.real_service, pytest.mark.ads]
if ads_config_from_env() is not None:
    pytestmark.append(pytest.mark.hardware)  # type: ignore[attr-defined]

_seq = 0


def _driver(ads: dict[str, Any]) -> ADSDriver:
    global _seq
    _seq += 1
    point = Point(
        point_id="readback",
        business_point_id="readback",
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"symbol": str(ads["read_symbol"]), "data_type": "REAL"},
    )
    table = PointTable(PointTableId("ads_race"), Protocol("ads"), {"readback": point})
    return ADSDriver(
        ConnectionEndpoint(str(ads["host"]), 851),
        table,
        {"target_net_id": f"127.1.0.{_seq}.1.1", "timeout": 3.0},
    )


async def _drain(batch: tuple[ProtocolSample, ...]) -> None:
    return None


@pytest.fixture
async def ads_local(ads_service: dict[str, Any]):
    await ADSDriver.initialize_local(
        ADSLocalConfig(
            local_ams_net_id=str(ads_service["local_net_id"]),
            local_ip=str(ads_service["local_ip"]),
        )
    )
    try:
        yield ads_service
    finally:
        try:
            with socket.create_connection(
                (str(ads_service["host"]), int(ads_service["port"])), timeout=0.2
            ):
                pass
        except OSError:
            ads_testserver.restart_testserver(ads_service)
        if not _TARGET_OWNERS:
            with contextlib.suppress(Exception):
                await ADSDriver.close_local()


class TestShutdownRaces:
    async def test_close_under_notification_flood_is_bounded_and_clean(
        self, ads_local: dict[str, Any]
    ) -> None:
        if ads_local["backend"] != "testserver":
            pytest.skip("需要直接驱动服务端 PLCVariable")
        driver = _driver(ads_local)
        await driver.connect()
        sub = driver.subscribe(("readback",), _drain, interval=0.01)
        await sub.start()

        variable = ads_local["variables"][str(ads_local["read_symbol"])]
        stop_flag = threading.Event()

        def flood() -> None:
            value = 0.0
            while not stop_flag.is_set():
                value += 1.0
                try:
                    variable.write(struct.pack("<f", value))
                except Exception:  # noqa: BLE001 - close 期间服务端对象可能正失效
                    return
                time.sleep(0.002)

        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        await asyncio.sleep(0.2)
        start = time.monotonic()
        try:
            await driver.close()
        finally:
            stop_flag.set()
            thread.join(timeout=2)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"通知洪流中 close 耗时 {elapsed:.2f}s 超预算"
        assert driver._state is ADSState.DISCONNECTED
        assert driver._identity not in _TARGET_OWNERS
        assert variable.notifications == []

    async def test_subscription_start_races_driver_close_20(
        self, ads_local: dict[str, Any]
    ) -> None:
        if ads_local["backend"] != "testserver":
            pytest.skip("需要直接检查服务端注册残留")
        variable = ads_local["variables"][str(ads_local["read_symbol"])]
        for round_index in range(20):
            driver = _driver(ads_local)
            await driver.connect()
            sub = driver.subscribe(("readback",), _drain, interval=0.01)
            start_result, close_result = await asyncio.gather(
                sub.start(), driver.close(), return_exceptions=True
            )
            # gather 能返回即证明竞争双方都在预算内结束；失败必须是显式异常而非挂起。
            assert start_result is None or isinstance(start_result, Exception)
            assert close_result is None or isinstance(close_result, Exception)
            assert driver._state in {ADSState.DISCONNECTED, ADSState.FAULTED}
            if round_index == 19:
                # 末轮给服务端一点时间处理在途 del，确认无注册残留。
                deadline = time.monotonic() + 2.0
                while variable.notifications and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                assert variable.notifications == []

    async def test_close_during_connect_is_bounded(self, ads_local: dict[str, Any]) -> None:
        driver = _driver(ads_local)
        connect_task = asyncio.create_task(driver.connect())
        await asyncio.sleep(0)  # 让建链任务进入调度，立即竞争 close
        with contextlib.suppress(Exception):
            await driver.close()  # CONNECTING 期 close 允许抛错，但必须有界
        with contextlib.suppress(Exception):
            await asyncio.wait_for(connect_task, timeout=5)
        # 若建链迟到成功，close 任务必须最终把连接收掉，不许留下 CONNECTED 孤儿。
        deadline = time.monotonic() + 5.0
        while driver.is_open() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert not driver.is_open()
        assert driver._state in {ADSState.DISCONNECTED, ADSState.FAULTED, ADSState.CLOSING}
