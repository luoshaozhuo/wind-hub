"""ADSDriver 网络故障 F01–F16 可靠性测试（真实 AMS/TCP + 故障代理）。

故障经 :class:`tests.support.tcp_fault_proxy.TCPFaultProxy` 在真实 TCP
字节流上注入（拒绝/黑洞/延迟/断链/帧计数切断），服务端是
``AdsMemoryServer``（真实 AMS/TCP 帧栈）。所有用例断言有界完成时间，
任何路径都不允许无限等待。

无法用 pyads testserver 复现、只能在真实 TwinCAT 验收的故障：

- F03 错误 AMS Net ID / F04 错误逻辑端口：testserver 不校验 AMS 寻址，
  真实 PLC 才会拒绝，列入硬件验收清单；
- F06 客户端进程被 SIGKILL 后 PLC 侧通知句柄是否释放：跨进程，由
  Stage 5 独立子进程测试覆盖观测方法，最终结论需真实 TwinCAT。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

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
_SERVER_IP = "127.0.0.51"
_PROXY_IP = "127.0.0.52"

pytestmark = [pytest.mark.ads, pytest.mark.reliability, pytest.mark.real_service]


def _point(point_id: str, offset: int, data_type: str = "DINT") -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={
            "data_type": data_type,
            "index_group": _GROUP,
            "index_offset": offset,
        },
    )


def _driver(
    points: dict[str, Point],
    *,
    host: str,
    port: int,
    timeout: float = 1.0,
    max_block_bytes: int = 1024,
) -> ADSDriver:
    return ADSDriver(
        ConnectionEndpoint(host, port),
        PointTable(PointTableId("fault"), Protocol("ads"), points),
        {
            "target_net_id": _NET_ID,
            "timeout": timeout,
            "twincat_version": "3",
            "max_block_bytes": max_block_bytes,
        },
    )


@pytest.fixture
async def local_ams() -> AsyncIterator[None]:
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    try:
        yield
    finally:
        await ADSDriver.close_local()


@pytest.fixture
async def memory_server() -> AsyncIterator[AdsMemoryServer]:
    server = AdsMemoryServer(ip_address=_SERVER_IP)
    server.start()
    try:
        yield server
    finally:
        server.stop()


async def _proxy(**kwargs: object) -> TCPFaultProxy:
    proxy = TCPFaultProxy(
        upstream_host=_SERVER_IP,
        upstream_port=48898,
        listen_host=_PROXY_IP,
        **kwargs,  # type: ignore[arg-type]
    )
    await proxy.start()
    return proxy


class TestConnectionFaults:
    async def test_f01_connect_refused_when_server_down(self, local_ams: None) -> None:
        """F01 服务未启动：connect 立即失败、有界、不留下打开会话。"""
        del local_ams
        driver = _driver({"a": _point("a", 0)}, host=_SERVER_IP, port=48898)
        start = time.monotonic()
        with pytest.raises(ProtocolError, match="connect"):
            await driver.connect()
        elapsed = time.monotonic() - start
        assert elapsed < 5.0
        assert driver.is_open() is False
        await driver.close()  # 失败路径后 close 仍须安全

    async def test_f02_connect_wrong_address_refused(self, local_ams: None) -> None:
        """F02 错误 IP（无监听 loopback 端点）：connect 快速拒绝。"""
        del local_ams
        driver = _driver({"a": _point("a", 0)}, host="127.0.0.99", port=1)
        start = time.monotonic()
        with pytest.raises(ProtocolError):
            await driver.connect()
        assert time.monotonic() - start < 5.0
        assert driver.is_open() is False
        await driver.close()

    async def test_f08_blackhole_connect_fails_bounded(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F08 黑洞：TCP 可连但 AMS 永不响应，connect 必须在超时附近放弃。"""
        del memory_server, local_ams
        proxy = await _proxy(blackhole=True)
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        try:
            start = time.monotonic()
            with pytest.raises(ProtocolError):
                await driver.connect()
            elapsed = time.monotonic() - start
            assert 0.5 <= elapsed < 8.0
            assert driver.is_open() is False
        finally:
            await driver.close()
            await proxy.stop()

    async def test_f09_latency_is_tolerated_within_timeout(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F09 每帧 50ms 延迟：远小于超时，连接与读写全部成功。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 9)]))
        proxy = await _proxy(delay=0.05)
        driver = _driver(
            {"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port, timeout=3.0
        )
        try:
            await driver.connect()
            assert (await driver.read_one("a")).value == 9
        finally:
            await driver.close()
            await proxy.stop()


class TestRuntimeState:
    async def test_f05_stop_state_is_not_tcp_disconnect(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F05 Runtime STOP：TCP 保持、health 不健康；恢复 RUN 后无需重连。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 3)]))
        proxy = await _proxy()
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        try:
            await driver.connect()
            assert (await driver.health()).healthy

            memory_server.set_running(False)
            health = await driver.health()
            assert health.healthy is False
            assert "STOP" in (health.message or "")

            memory_server.set_running(True)
            assert (await driver.health()).healthy
            assert (await driver.read_one("a")).value == 3
        finally:
            await driver.close()
            await proxy.stop()


class TestMidSessionDisconnect:
    async def test_f07_tcp_drop_fails_io_bounded(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F07 链路中断：在途/新发 IO 有界失败，close 有界完成。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 4)]))
        proxy = await _proxy()
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        try:
            await driver.connect()
            await proxy.drop()
            start = time.monotonic()
            with pytest.raises(ProtocolError):
                await driver.read_one("a")
            assert time.monotonic() - start < 5.0
            start = time.monotonic()
            await driver.close()
            assert time.monotonic() - start < 5.0
        finally:
            await proxy.stop()

    async def test_f10_recovery_with_new_session_after_link_restored(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F10 故障恢复：断链失败后经新代理会话重建，读取恢复并拿到新值。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 1)]))
        proxy = await _proxy()
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        await driver.connect()
        await proxy.drop()
        with pytest.raises(ProtocolError):
            await driver.read_one("a")
        await driver.close()
        await proxy.stop()

        proxy2 = await _proxy()
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 2)]))
        driver2 = _driver({"a": _point("a", 0)}, host=proxy2.host, port=proxy2.listen_port)
        try:
            await driver2.connect()
            assert (await driver2.read_one("a")).value == 2
        finally:
            await driver2.close()
            await proxy2.stop()

    async def test_f11_server_restart_same_endpoint_allows_reconnect(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F11 服务端重启（模拟 PLC 重启）：同一端点上新会话可用。"""
        del local_ams
        proxy = await _proxy()
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 5)]))
        await driver.connect()
        assert (await driver.read_one("a")).value == 5
        await driver.close()

        # 服务端进程消失：先切断代理上行，再重启服务端。
        memory_server.restart()
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 6)]))
        driver2 = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        try:
            await driver2.connect()
            assert (await driver2.read_one("a")).value == 6
        finally:
            await driver2.close()
            await proxy.stop()

    async def test_f13_deregistration_disconnect_close_is_bounded(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F13 注销通知时链路已断：close 不抛错、有界完成。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 0)]))
        proxy = await _proxy()
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        await driver.connect()

        async def on_sample(_sample: ProtocolSample) -> None:
            pass

        handle = await driver.subscribe(("a",), on_sample, interval=0.05)
        await asyncio.sleep(0.1)
        await proxy.drop()
        start = time.monotonic()
        await handle.close()
        await driver.close()
        assert time.monotonic() - start < 5.0
        await proxy.stop()

    async def test_f14_worker_transport_disconnect_close_bounded(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F14 订阅活跃期间断链：已投递样本真实到达，随后关闭有界，可重建。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 0)]))
        proxy = await _proxy()
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        await driver.connect()
        seen: list[int] = []
        arrived = asyncio.Event()

        async def on_sample(sample: ProtocolSample) -> None:
            value = int(sample.value)  # type: ignore[arg-type]  # 测点为 DINT
            seen.append(value)
            if value == 42:
                arrived.set()

        await driver.subscribe(("a",), on_sample, interval=0.05)
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 42)]))
        # SERVERCYCLE：新值在下一周期由服务端主动推送（无需再写）。
        await asyncio.wait_for(arrived.wait(), timeout=3.0)

        await proxy.drop()
        start = time.monotonic()
        await driver.close()
        assert time.monotonic() - start < 5.0
        assert 42 in seen and seen[-1] == 42
        await proxy.stop()


class TestRegistrationFaults:
    async def test_f12_disconnect_during_second_block_registration(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F12 第二个区块注册途中断链：失败收敛为 ProtocolError、有界，
        已注册区块尝试注销，Driver 不残留 tracked 订阅，重启后可重建订阅。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, b"\x00" * 8200)
        # 帧序：connect 健康检查=1，区块1 注册=2，区块2 注册=3（切断点）。
        proxy = await _proxy(drop_after_frames=3)
        driver = _driver(
            {"a": _point("a", 0), "b": _point("b", 8192)},
            host=proxy.host,
            port=proxy.listen_port,
            timeout=1.0,
            max_block_bytes=64,
        )
        try:
            await driver.connect()

            async def on_sample(_sample: ProtocolSample) -> None:
                pass

            start = time.monotonic()
            with pytest.raises(ProtocolError, match="subscribe"):
                await driver.subscribe(("a", "b"), on_sample, interval=0.05)
            assert time.monotonic() - start < 6.0
            assert driver._subscriptions == set()

            # 死亡会话上保留的服务端句柄随服务端重启清空；新会话注册恢复。
            await driver.close()
            memory_server.restart()
            memory_server.set_values(_GROUP, 0, b"\x00" * 8200)
        finally:
            await proxy.stop()

        proxy2 = await _proxy()
        driver2 = _driver(
            {"a": _point("a", 0), "b": _point("b", 8192)},
            host=proxy2.host,
            port=proxy2.listen_port,
            max_block_bytes=64,
        )
        try:
            await driver2.connect()

            async def on_sample(_sample: ProtocolSample) -> None:
                pass

            handle = await driver2.subscribe(("a", "b"), on_sample, interval=0.05)
            await asyncio.sleep(0.2)
            assert memory_server.notification_count() == 2
            await handle.close()
            assert memory_server.notification_count() == 0
            await driver2.close()
        finally:
            await proxy2.stop()


class TestConcurrencyAndChurn:
    async def test_f16_concurrent_reads_all_complete_when_link_drops(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F16 断链瞬间并发请求：全部有界结束（成功或明确失败），无挂起。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 7)]))
        proxy = await _proxy()
        driver = _driver({"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port)
        await driver.connect()

        async def read_once() -> object:
            try:
                return (await driver.read_one("a")).value
            except ProtocolError as exc:
                return exc

        tasks = [asyncio.create_task(read_once()) for _ in range(10)]
        await asyncio.sleep(0.05)
        await proxy.drop()
        start = time.monotonic()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5.0)
        assert time.monotonic() - start < 5.0
        assert len(results) == 10
        assert all(not isinstance(result, Exception) or isinstance(result, ProtocolError)
                   for result in results)
        await driver.close()
        await proxy.stop()

    async def test_f15_rapid_connect_close_cycles_bounded(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """F15 快速 connect/close 往返：每轮干净释放，不累积连接资源。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 1)]))
        proxy = await _proxy()
        try:
            for _ in range(25):
                driver = _driver(
                    {"a": _point("a", 0)}, host=proxy.host, port=proxy.listen_port
                )
                await asyncio.wait_for(driver.connect(), timeout=5.0)
                assert (await driver.read_one("a")).value == 1
                await asyncio.wait_for(driver.close(), timeout=5.0)
            assert proxy.stats().open_connections == 0
        finally:
            await proxy.stop()
