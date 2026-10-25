"""ADS 资源循环稳定性：连接/订阅/服务端重启循环下的句柄与身份泄漏。

- 替身连接（``mock_service``）：100 轮 connect/close 与 50 轮
  subscribe start/stop，核对 Net ID 占用表、notification 注册/反注册、
  本地端口开关计数；
- 真实 AMS/TCP 栈（``real_service``）：10 轮服务端重启后同端点重连。
真实 SIGKILL 残留场景仍需硬件/子进程 harness，本轮 NOT RUN。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import pytest

from core.application.settings import ADSLocalConfig
from core.infrastructure.protocol.ads import ADSDriver
from core.infrastructure.protocol.ads.driver import _TARGET_OWNERS
from tests.support import ads_testserver
from tests.support.ads_fake import FakePyadsConnection, make_driver, make_point, patch_session
from tests.support.env import ads_config_from_env

pytestmark = [pytest.mark.ads]

POINTS = {
    "a": make_point("a", data_type="REAL", index_group=0x4020, index_offset=0, size=4),
    "b": make_point("b", data_type="REAL", index_group=0x4020, index_offset=4, size=4),
}


async def _noop(batch: tuple[Any, ...]) -> None:
    return None


class TestFakeResourceCycles:
    """替身级快速循环：精确核对句柄/占用表的增删平衡（mock_service）。"""

    async def test_100_connect_close_cycles_release_identity(
        self, ads_identity: ADSLocalConfig, fake_pyads: Any
    ) -> None:
        driver = make_driver(POINTS, net_id="192.0.2.10.1.1")
        for _ in range(100):
            connection = FakePyadsConnection()
            patch_session(driver, connection)
            await driver.connect()
            assert driver.is_open()
            assert connection.opened == 1
            await driver.close()
            assert connection.closed_calls == 1
            assert driver._identity not in _TARGET_OWNERS
        # 本地端口全程只开一次，close_local 后恰好关闭一次。
        assert fake_pyads.opened_ports == 1
        await ADSDriver.close_local()
        assert fake_pyads.closed_ports == 1

    async def test_50_subscription_start_stop_cycles_balance_handles(
        self, ads_identity: ADSLocalConfig
    ) -> None:
        driver = make_driver(POINTS, net_id="192.0.2.10.1.1")
        connection = FakePyadsConnection()
        patch_session(driver, connection)
        await driver.connect()
        for _ in range(50):
            sub = driver.subscribe(("a", "b"), _noop, interval=0.01)
            await sub.start()
            await sub.stop()
            # 原生句柄必须每轮平衡（集合清空只在 driver.close 发生，见 P2 记录）。
            assert connection.added == connection.removed
        assert connection.added == 50  # 相邻点合并为一个通知块，每轮注册 1 次
        await driver.close()
        assert driver._subscriptions == set()
        assert connection.closed_calls == 1

    async def test_double_close_is_idempotent(self, ads_identity: ADSLocalConfig) -> None:
        driver = make_driver(POINTS, net_id="192.0.2.10.1.1")
        connection = FakePyadsConnection()
        patch_session(driver, connection)
        await driver.connect()
        await asyncio.gather(driver.close(), driver.close(), driver.close())
        assert connection.closed_calls == 1
        assert driver._identity not in _TARGET_OWNERS


class TestRealServerRestartCycles:
    async def test_10_server_restart_cycles_reconnect(self, ads_service: dict[str, Any]) -> None:
        if ads_service["backend"] != "testserver":
            pytest.skip("需要直接控制 AdsTestServer 生命周期")
        await ADSDriver.initialize_local(
            ADSLocalConfig(
                local_ams_net_id=str(ads_service["local_net_id"]),
                local_ip=str(ads_service["local_ip"]),
            )
        )

        def fresh_driver(cycle: int) -> ADSDriver:
            from core.domain import (
                ConnectionEndpoint,
                Point,
                PointAccess,
                PointTable,
                PointTableId,
                Protocol,
            )
            from tests.support.units import NONE_UNIT

            point = Point(
                point_id="readback",
                business_point_id="readback",
                source_unit=NONE_UNIT,
                access=PointAccess.READ_WRITE,
                ext={
                    "symbol": str(ads_service["read_symbol"]),
                    "data_type": "REAL",
                },
            )
            table = PointTable(
                PointTableId("ads_restart"),
                Protocol("ads"),
                {"readback": point},
            )
            return ADSDriver(
                ConnectionEndpoint(str(ads_service["host"]), 851),
                table,
                {"target_net_id": f"127.0.1.{cycle}.1.1", "timeout": 3.0},
            )

        start = time.monotonic()
        try:
            for cycle in range(10):
                driver = fresh_driver(cycle)
                await asyncio.wait_for(driver.connect(), timeout=5)
                assert driver.is_open()
                await driver.close()
                ads_testserver.restart_testserver(ads_service)
        finally:
            with contextlib.suppress(Exception):
                await ADSDriver.close_local()
            ads_testserver.restart_testserver(ads_service)
        assert time.monotonic() - start < 60.0


# 服务真实性必须显式声明（conftest 不按目录默认赋 real_service）。
TestFakeResourceCycles = pytest.mark.mock_service(TestFakeResourceCycles)
TestRealServerRestartCycles = pytest.mark.real_service(TestRealServerRestartCycles)
if ads_config_from_env() is not None:
    TestRealServerRestartCycles = pytest.mark.hardware(TestRealServerRestartCycles)
