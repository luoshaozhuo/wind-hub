"""ADS 链路故障可靠性：拒连、连接中断链、并发 IO、快速循环与重启重连。

所有用例跑在真实 AMS/TCP 栈（pyads AdsTestServer，``real_service``）上，
不使用替身连接；替身级故障注入在 component 层覆盖。
需要 root + tc netem 的黑洞/时延场景（旧 F08/F09）本环境 NOT RUN。
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from core.application.protocol_contract import Quality
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
from core.infrastructure.protocol.ads.session import ADSResourceUncertainError
from tests.support import ads_testserver
from tests.support.env import ads_config_from_env
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.real_service, pytest.mark.ads]
if ads_config_from_env() is not None:
    pytestmark.append(pytest.mark.hardware)  # type: ignore[attr-defined]


def _point(point_id: str, symbol: str) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=point_id,
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"symbol": symbol, "data_type": "REAL"},
    )


#: 每个用例/实例使用独立目标 Net ID：FAULTED 占用按设计不可回收，
#: 不能让一个用例的 fail-closed 残留阻断同进程后续用例（testserver 不校验 Net ID）。
_net_seq = 0


def _driver(
    ads: dict[str, Any], *, host: str | None = None, net_id: str | None = None
) -> ADSDriver:
    global _net_seq
    _net_seq += 1
    target_net = net_id or f"127.0.0.{_net_seq}.1.1"
    points = PointTable(
        PointTableId("ads_rel"),
        Protocol("ads"),
        {"readback": _point("readback", str(ads["read_symbol"]))},
    )
    return ADSDriver(
        ConnectionEndpoint(host or str(ads["host"]), 851),
        points,
        {"target_net_id": target_net, "timeout": 3.0, "route_auto_add": False},
    )


@pytest.fixture
async def ads_local(ads_service: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    await ADSDriver.initialize_local(
        ADSLocalConfig(
            local_ams_net_id=str(ads_service["local_net_id"]),
            local_ip=str(ads_service["local_ip"]),
        )
    )
    try:
        yield ads_service
    finally:
        # 断链类用例即使失败也必须把 testserver 恢复，避免污染同文件后续用例。
        try:
            with socket.create_connection(
                (str(ads_service["host"]), int(ads_service["port"])), timeout=0.2
            ):
                pass
        except OSError:
            ads_testserver.restart_testserver(ads_service)
        if not _TARGET_OWNERS:
            await ADSDriver.close_local()


class TestConnectionFaults:
    async def test_f01_connect_refused_is_bounded_and_fail_closed(
        self, ads_local: dict[str, Any]
    ) -> None:
        if ads_local["backend"] != "testserver":
            pytest.skip("拒连注入依赖本机回环别名 127.0.0.2")
        # 127.0.0.2:48898 上无监听（testserver 只绑 127.0.0.1），TCP RST 立即拒连。
        driver = _driver(ads_local, host="127.0.0.2", net_id="127.0.99.2.1.1")
        start = time.monotonic()
        # D2：明确的物理 open 失败（pyads 已自回滚本地端口）必须与“物理
        # 状态不明”区分——显式异常上抛，但资源确定收敛，不再是 uncertain。
        with pytest.raises(Exception) as exc_info:
            await driver.connect()
        assert not isinstance(exc_info.value, ADSResourceUncertainError)
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, f"拒连未在预算内收敛: {elapsed:.2f}s"
        assert not driver.is_open()
        health = await driver.health()
        assert not health.healthy  # 绝无假 GOOD
        # 确定失败 → DISCONNECTED 并释放 Net ID，允许 Watchdog 后续重连。
        assert driver._state is ADSState.DISCONNECTED
        assert "127.0.99.2.1.1" not in _TARGET_OWNERS
        # 同一 driver 修复端点后可重新建链（重连语义实证）。
        recovered = _driver(ads_local, net_id="127.0.99.2.1.1")
        await recovered.connect()
        assert recovered.is_open()
        await recovered.close()
        # 拒连期间不影响其他 Net ID 的正常建链。
        healthy = _driver(ads_local)
        await healthy.connect()
        assert healthy.is_open()
        await healthy.close()


class TestMidSessionDisconnect:
    async def test_f07_f16_drop_concurrent_io_bounded(self, ads_local: dict[str, Any]) -> None:
        if ads_local["backend"] != "testserver":
            pytest.skip("需要直接控制 AdsTestServer 生命周期")
        driver = _driver(ads_local)
        await driver.connect()
        assert driver.is_open()

        async def read_loop(stop: asyncio.Event) -> int:
            failures = 0
            while not stop.is_set():
                try:
                    sample = await driver.read_one("readback")
                except Exception:  # noqa: BLE001 - 断链/关闭期命令被拒均算显式失败
                    failures += 1
                    await asyncio.sleep(0)
                    continue
                if sample.quality is not Quality.GOOD:
                    failures += 1
                await asyncio.sleep(0)
            return failures

        stop = asyncio.Event()
        readers = [asyncio.create_task(read_loop(stop)) for _ in range(8)]
        await asyncio.sleep(0.2)
        ads_local["server"].stop()
        await asyncio.sleep(0.3)
        deadline = time.monotonic() + 5.0
        close_error: Exception | None = None
        try:
            await driver.close()
        except Exception as exc:  # noqa: BLE001 - 断链后 close 允许 fail-closed
            close_error = exc
        stop.set()
        failures = await asyncio.gather(*readers)
        assert time.monotonic() < deadline
        assert sum(failures) > 0  # 断链后读必须显式失败，不许假 GOOD
        assert driver._state in {ADSState.DISCONNECTED, ADSState.FAULTED}
        if driver._state is ADSState.FAULTED:
            assert close_error is not None  # FAULTED 必须伴随显式错误
        else:
            assert driver._identity not in _TARGET_OWNERS

    async def test_f11_server_restart_same_endpoint_allows_reconnect(
        self, ads_local: dict[str, Any]
    ) -> None:
        if ads_local["backend"] != "testserver":
            pytest.skip("需要直接控制 AdsTestServer 生命周期")
        driver = _driver(ads_local)
        await driver.connect()
        await driver.close()
        ads_testserver.restart_testserver(ads_local)
        rebuilt = _driver(ads_local)
        await asyncio.wait_for(rebuilt.connect(), timeout=5)
        assert rebuilt.is_open()
        sample = await rebuilt.read_one("readback")
        assert sample.quality is Quality.GOOD
        await rebuilt.close()


class TestConcurrencyAndChurn:
    async def test_f15_rapid_connect_close_cycles_bounded(self, ads_local: dict[str, Any]) -> None:
        if ads_local["backend"] != "testserver":
            pytest.skip("50 轮快速循环只在本机 testserver 上执行")
        start = time.monotonic()
        for _ in range(50):
            driver = _driver(ads_local)
            await driver.connect()
            assert driver.is_open()
            await driver.close()
            assert driver._state is ADSState.DISCONNECTED
            assert driver._identity not in _TARGET_OWNERS
        elapsed = time.monotonic() - start
        assert elapsed < 30.0, f"50 轮 connect/close 耗时 {elapsed:.1f}s 超预算"
