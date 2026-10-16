"""ADS Driver / Subscription 关闭、注销、重连并发竞态压力测试。

所有用例在真实 AMS/TCP 栈（``AdsMemoryServer`` + ``TCPFaultProxy``）上
强制并发与故障交错，断言每条路径有界完成、无异常逃逸、无句柄/连接/
asyncio 任务/线程累积：

- Driver 直接关闭活跃订阅（上层不先关订阅句柄）；
- tracked handle.close() 与 driver.close() 并发；
- subscribe() 注册途中与 driver.close() 并发；
- 断链瞬间关闭活跃订阅；
- 服务端重启后新会话重新订阅并再次收到真实通知；
- close 幂等（重复调用安全）。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator

import pytest
from pyads.pyads_ex import callback_store  # type: ignore[import-untyped]

from core.application.errors import ProtocolError
from core.application.protocol_contract import ProtocolSample
from core.application.settings import ADSLocalConfig
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import ADSDriver
from tests.support.ads_memory_server import AdsMemoryServer, pack_points
from tests.support.tcp_fault_proxy import TCPFaultProxy
from tests.support.units import NONE_UNIT

_GROUP = 0x4020
_NET_ID = "127.0.0.1.2.1"
_LOCAL_NET_ID = "127.0.0.1.1.1"
_SERVER_IP = "127.0.0.81"
_PROXY_IP = "127.0.0.82"

pytestmark = [pytest.mark.ads, pytest.mark.reliability, pytest.mark.real_service]


def _point(point_id: str, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "DINT", "index_group": _GROUP, "index_offset": offset},
    )


def _driver(host: str, *, max_block_bytes: int = 1024) -> ADSDriver:
    points = {"a": _point("a", 0), "b": _point("b", 8192)}
    return ADSDriver(
        ConnectionEndpoint(host, 48898),
        PointTable(PointTableId("races"), Protocol("ads"), points),
        {
            "target_net_id": _NET_ID,
            "timeout": 1.0,
            "twincat_version": "3",
            "max_block_bytes": max_block_bytes,
        },
    )


async def _ignore_sample(_sample: ProtocolSample) -> None:
    pass


async def _readback_callback(driver: ADSDriver, sample: ProtocolSample) -> None:
    """回调内再走驱动 IO：Worker 取消与 IO 锁不得自锁。"""
    with contextlib.suppress(ProtocolError):
        await driver.read_one(sample.point_id)


async def _close_with_active_subscription_cycle(server: AdsMemoryServer) -> None:
    driver = _driver(_SERVER_IP)

    async def on_sample(sample: ProtocolSample) -> None:
        await _readback_callback(driver, sample)

    await asyncio.wait_for(driver.connect(), timeout=5.0)
    await driver.subscribe(("a",), on_sample, interval=0.02)
    await asyncio.sleep(0.05)
    await asyncio.wait_for(driver.close(), timeout=5.0)
    assert server.notification_count() == 0


async def _concurrent_close_cycle(server: AdsMemoryServer) -> None:
    driver = _driver(_SERVER_IP, max_block_bytes=64)
    await asyncio.wait_for(driver.connect(), timeout=5.0)
    handle = await driver.subscribe(("a", "b"), _ignore_sample, interval=0.02)
    await asyncio.sleep(0.02)
    results = await asyncio.wait_for(
        asyncio.gather(
            handle.close(),
            driver.close(),
            return_exceptions=True,
        ),
        timeout=5.0,
    )
    assert results == [None, None], results
    assert server.notification_count() == 0


async def _subscribe_close_race_cycle(server: AdsMemoryServer) -> None:
    driver = _driver(_SERVER_IP, max_block_bytes=64)
    await asyncio.wait_for(driver.connect(), timeout=5.0)

    async def subscribe_or_fail() -> str:
        try:
            await driver.subscribe(("a", "b"), _ignore_sample, interval=0.05)
            return "subscribed"
        except ProtocolError:
            return "failed"

    sub_task = asyncio.create_task(subscribe_or_fail())
    # 不固定交错点：让 close 与注册在锁上竞争。
    await asyncio.sleep(0.0005)
    close_task = asyncio.create_task(driver.close())
    outcome, close_result = await asyncio.wait_for(
        asyncio.gather(sub_task, close_task, return_exceptions=True),
        timeout=8.0,
    )
    assert close_result is None, close_result
    assert not isinstance(outcome, BaseException), outcome
    assert outcome in {"subscribed", "failed"}
    # 两条路径都必须把服务端注册清干净（健康连接上 DEL 必达）。
    assert server.notification_count() == 0, outcome


async def _restart_resubscribe_cycle(server: AdsMemoryServer, index: int) -> None:
    server.restart()
    server.set_values(_GROUP, 0, pack_points([("DINT", index)]))
    driver = _driver(_SERVER_IP)
    seen: list[int] = []
    done = asyncio.Event()

    async def on_sample(sample: ProtocolSample) -> None:
        value = int(sample.value)  # type: ignore[arg-type]  # 测点为 DINT
        seen.append(value)
        if value == index:
            done.set()

    await asyncio.wait_for(driver.connect(), timeout=5.0)
    handle = await driver.subscribe(("a",), on_sample, interval=0.02)
    try:
        await asyncio.wait_for(done.wait(), timeout=4.0)
    finally:
        await handle.close()
        await asyncio.wait_for(driver.close(), timeout=5.0)
    assert server.notification_count() == 0


@pytest.fixture
async def local_ams() -> AsyncIterator[None]:
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    try:
        yield
    finally:
        await ADSDriver.close_local()


@pytest.fixture
async def server() -> AsyncIterator[AdsMemoryServer]:
    ads_server = AdsMemoryServer(ip_address=_SERVER_IP)
    ads_server.start()
    try:
        yield ads_server
    finally:
        ads_server.stop()


@pytest.fixture
async def stack() -> AsyncIterator[tuple[AdsMemoryServer, TCPFaultProxy]]:
    ads_server = AdsMemoryServer(ip_address=_SERVER_IP)
    ads_server.start()
    proxy = TCPFaultProxy(
        upstream_host=_SERVER_IP,
        upstream_port=48898,
        listen_host=_PROXY_IP,
    )
    await proxy.start()
    try:
        yield ads_server, proxy
    finally:
        await proxy.stop()
        ads_server.stop()


class TestShutdownRaces:
    async def test_driver_close_owns_active_subscription_cleanup_30(
        self, server: AdsMemoryServer, local_ams: None
    ) -> None:
        """上层只关 Driver 不关订阅句柄：30 轮全部有界、零残留。"""
        del local_ams
        server.set_values(_GROUP, 0, pack_points([("DINT", 1)]))
        threads_start = threading.active_count()
        for _ in range(30):
            await _close_with_active_subscription_cycle(server)
        assert len(callback_store) == 0
        assert threading.active_count() <= threads_start + 2

    async def test_concurrent_handle_close_and_driver_close_30(
        self, server: AdsMemoryServer, local_ams: None
    ) -> None:
        """handle.close() 与 driver.close() 同时发生：幂等、有界、不抛错。"""
        del local_ams
        server.set_values(_GROUP, 0, b"\x00" * 9000)
        for _ in range(30):
            await _concurrent_close_cycle(server)
        assert len(callback_store) == 0

    async def test_subscribe_races_with_close_30(
        self, server: AdsMemoryServer, local_ams: None
    ) -> None:
        """注册途中与 close 并发：要么注册成功后被 close 清掉，要么明确失败。"""
        del local_ams
        server.set_values(_GROUP, 0, b"\x00" * 9000)
        for _ in range(30):
            await _subscribe_close_race_cycle(server)
        assert len(callback_store) == 0

    async def test_drop_then_close_active_subscription_bounded_30(
        self,
        stack: tuple[AdsMemoryServer, TCPFaultProxy],
        local_ams: None,
    ) -> None:
        """活跃订阅期间断链后关闭：30 轮全部有界完成，新会话可重建订阅。"""
        del local_ams
        server, proxy = stack
        server.set_values(_GROUP, 0, b"\x00" * 9000)
        current = proxy
        for _ in range(30):
            driver = _driver(_PROXY_IP, max_block_bytes=64)
            await asyncio.wait_for(driver.connect(), timeout=5.0)
            await driver.subscribe(("a", "b"), _ignore_sample, interval=0.05)
            await asyncio.sleep(0.02)
            await current.drop()
            await asyncio.wait_for(driver.close(), timeout=5.0)
            # 重建代理模拟链路恢复（上行服务未重启）。
            await current.stop()
            current = TCPFaultProxy(
                upstream_host=_SERVER_IP,
                upstream_port=48898,
                listen_host=_PROXY_IP,
            )
            await current.start()
        # 断链轮次的 DEL 不可达，死会话句柄按 testserver 行为残留（真实
        # PLC 是否回收须硬件验收）；末轮在新代理上重新订阅，活跃注册必须
        # 恰好是本轮的两个，并能干净注销。
        residue = len(server.residue_registrations())
        assert residue == 60, residue
        driver = _driver(_PROXY_IP, max_block_bytes=64)
        await asyncio.wait_for(driver.connect(), timeout=5.0)
        handle = await driver.subscribe(("a", "b"), _ignore_sample, interval=0.05)
        try:
            await asyncio.sleep(0.1)
            assert len(server.residue_registrations()) == residue
            assert server.notification_count() - residue == 2
        finally:
            await handle.close()
            await driver.close()
        # 新会话的 DEL 经健康链路送达：残留数不变，无新增泄漏。
        assert server.notification_count() == residue
        await current.stop()

    async def test_restart_resubscribe_redelivers_30(
        self, server: AdsMemoryServer, local_ams: None
    ) -> None:
        """30 次服务重启 + 重连 + 重订阅：每轮重新收到真实周期数据。"""
        del local_ams
        for index in range(30):
            await _restart_resubscribe_cycle(server, index)
        assert len(callback_store) == 0

    async def test_double_close_is_idempotent(
        self, server: AdsMemoryServer, local_ams: None
    ) -> None:
        """Driver 与订阅句柄重复关闭都安全，且有界。"""
        del local_ams
        server.set_values(_GROUP, 0, pack_points([("DINT", 1)]))
        driver = _driver(_SERVER_IP)
        await asyncio.wait_for(driver.connect(), timeout=5.0)
        handle = await driver.subscribe(("a",), _ignore_sample, interval=0.05)
        await asyncio.sleep(0.05)
        await asyncio.wait_for(handle.close(), timeout=5.0)
        await asyncio.wait_for(handle.close(), timeout=5.0)
        await asyncio.wait_for(driver.close(), timeout=5.0)
        await asyncio.wait_for(driver.close(), timeout=5.0)
        assert server.notification_count() == 0
        assert len(callback_store) == 0
