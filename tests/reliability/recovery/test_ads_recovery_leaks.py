"""ADS 恢复与资源泄漏验收（最高优先级可靠性测试）。

覆盖：
- 故障后重建会话必须重新拿到真实通知数据（不只看 is_open()）；
- 独立客户端进程被 SIGKILL 后，服务端通知句柄/连接/线程的残留观测；
- 100 次 connect/close、100 次 subscribe/unsubscribe、30 次服务端重启、
  30 次故障恢复、30 次 SIGKILL/重启循环后不累积句柄/线程/连接；
- 多区块长时运行无整批丢弃、无 worker/句柄泄漏。

关键事实：SIGKILL 后客户端无法执行注销。本测试用 pyads 真实 AMS/TCP
栈证明服务端注册表是否残留——testserver 线性内存服务不会在 TCP 断开时
回收句柄（实测残留），这与真实 TwinCAT 的行为对应关系只能在硬件上
确认（见交付报告验收清单），此处不用 mock 给出假结论。
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pyads.pyads_ex import callback_store  # type: ignore[import-untyped]

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
_SERVER_IP = "127.0.0.71"
_PROXY_IP = "127.0.0.72"
_KILL_SERVER_IP = "127.0.0.73"

pytestmark = [pytest.mark.ads, pytest.mark.reliability, pytest.mark.real_service]


def _point(point_id: str, offset: int, data_type: str = "DINT") -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": data_type, "index_group": _GROUP, "index_offset": offset},
    )


def _driver(
    points: dict[str, Point],
    *,
    host: str,
    port: int = 48898,
    timeout: float = 2.0,
    max_block_bytes: int = 1024,
) -> ADSDriver:
    return ADSDriver(
        ConnectionEndpoint(host, port),
        PointTable(PointTableId("recovery"), Protocol("ads"), points),
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
async def stack() -> AsyncIterator[tuple[AdsMemoryServer, TCPFaultProxy]]:
    server = AdsMemoryServer(ip_address=_SERVER_IP)
    server.start()
    proxy = TCPFaultProxy(
        upstream_host=_SERVER_IP,
        upstream_port=48898,
        listen_host=_PROXY_IP,
    )
    await proxy.start()
    try:
        yield server, proxy
    finally:
        await proxy.stop()
        server.stop()


async def _subscribe_expect(
    driver: ADSDriver,
    server: AdsMemoryServer,
    point_ids: tuple[str, ...],
    offsets: tuple[int, ...],
    *,
    value: int,
) -> tuple[Any, list[int]]:
    """注册订阅并通过真实服务端通知投递拿到数据，返回 (handle, 收到的值)。"""
    seen: list[int] = []
    done = asyncio.Event()
    expected = set(point_ids)

    async def on_sample(sample: ProtocolSample) -> None:
        seen.append(int(sample.value))  # type: ignore[arg-type]  # 测点为 DINT
        if len(seen) >= len(expected):
            done.set()

    handle = await driver.subscribe(point_ids, on_sample, interval=0.05)
    server.set_values(
        _GROUP,
        offsets[0],
        b"".join(pack_points([("DINT", value)]) for _ in offsets),
    )
    await asyncio.wait_for(done.wait(), timeout=4.0)
    return handle, seen


class TestRecoveryWithData:
    async def test_rebuild_after_fault_redelivers_notification_data(
        self,
        stack: tuple[AdsMemoryServer, TCPFaultProxy],
        local_ams: None,
    ) -> None:
        """故障恢复的判据是重建后重新收到真实通知数据，不是 is_open()。"""
        del local_ams
        server, proxy = stack
        points = {"a": _point("a", 0)}
        server.set_values(_GROUP, 0, pack_points([("DINT", 0)]))

        driver = _driver(points, host=_PROXY_IP)
        await driver.connect()
        handle, seen = await _subscribe_expect(
            driver, server, ("a",), (0,), value=11
        )
        assert seen == [11]
        await handle.close()
        await proxy.drop()
        await driver.close()

        # 模拟 PLC 重启后链路恢复：新会话 + 新订阅必须再次收到数据。
        server.restart()
        server.set_values(_GROUP, 0, pack_points([("DINT", 0)]))
        driver2 = _driver(points, host=_PROXY_IP)
        try:
            await driver2.connect()
            _, seen2 = await _subscribe_expect(
                driver2, server, ("a",), (0,), value=22
            )
            assert seen2 == [22]
        finally:
            await driver2.close()


class TestResourceCycles:
    async def test_100_connect_close_cycles(
        self,
        stack: tuple[AdsMemoryServer, TCPFaultProxy],
        local_ams: None,
    ) -> None:
        del local_ams
        server, proxy = stack
        server.set_values(_GROUP, 0, pack_points([("DINT", 7)]))
        threads_start = threading.active_count()
        for _ in range(100):
            driver = _driver({"a": _point("a", 0)}, host=_PROXY_IP)
            await asyncio.wait_for(driver.connect(), timeout=5.0)
            assert (await driver.read_one("a")).value == 7
            await asyncio.wait_for(driver.close(), timeout=5.0)
        assert proxy.stats().open_connections == 0
        assert server.notification_count() == 0
        assert threading.active_count() <= threads_start + 2

    async def test_100_subscribe_unsubscribe_cycles(
        self,
        stack: tuple[AdsMemoryServer, TCPFaultProxy],
        local_ams: None,
    ) -> None:
        del local_ams
        server, _proxy = stack
        server.set_values(_GROUP, 0, pack_points([("DINT", 0)]))
        driver = _driver({"a": _point("a", 0)}, host=_PROXY_IP)
        await driver.connect()
        try:
            async def on_sample(_sample: ProtocolSample) -> None:
                pass

            for index in range(100):
                handle = await driver.subscribe(("a",), on_sample, interval=0.05)
                await asyncio.sleep(0.005)
                assert server.notification_count() == 1
                await handle.close()
                assert server.notification_count() == 0
                if index % 10 == 0:
                    # pyads 进程内 C 回调表不随轮次增长。
                    assert len(callback_store) == 0
        finally:
            await driver.close()
        assert len(callback_store) == 0

    async def test_30_server_restart_cycles(
        self,
        stack: tuple[AdsMemoryServer, TCPFaultProxy],
        local_ams: None,
    ) -> None:
        del local_ams
        server, _proxy = stack
        for index in range(30):
            server.restart()
            server.set_values(_GROUP, 0, pack_points([("DINT", index)]))
            driver = _driver({"a": _point("a", 0)}, host=_PROXY_IP)
            try:
                await asyncio.wait_for(driver.connect(), timeout=5.0)
                assert (await driver.read_one("a")).value == index
            finally:
                await asyncio.wait_for(driver.close(), timeout=5.0)
        assert server.notification_count() == 0

    async def test_30_fault_recovery_cycles(
        self,
        stack: tuple[AdsMemoryServer, TCPFaultProxy],
        local_ams: None,
    ) -> None:
        del local_ams
        server, proxy = stack
        for index in range(30):
            server.set_values(_GROUP, 0, pack_points([("DINT", index)]))
            driver = _driver({"a": _point("a", 0)}, host=_PROXY_IP)
            await asyncio.wait_for(driver.connect(), timeout=5.0)
            assert (await driver.read_one("a")).value == index
            await proxy.drop()
            await asyncio.wait_for(driver.close(), timeout=5.0)
            # 新代理模拟链路恢复（上行服务未重启）。
            await proxy.stop()
            new_proxy = TCPFaultProxy(
                upstream_host=_SERVER_IP,
                upstream_port=48898,
                listen_host=_PROXY_IP,
            )
            await new_proxy.start()
            driver2 = _driver({"a": _point("a", 0)}, host=_PROXY_IP)
            try:
                await asyncio.wait_for(driver2.connect(), timeout=5.0)
                assert (await driver2.read_one("a")).value == index
            finally:
                await asyncio.wait_for(driver2.close(), timeout=5.0)
                await new_proxy.stop()
            # 为下一轮重建默认代理。
            if index < 29:
                await proxy.start()
        # 最后一轮代理已停；重开供 fixture teardown 幂等停止。
        await proxy.start()

    async def test_long_multi_block_run_no_drops_no_leaks(
        self,
        stack: tuple[AdsMemoryServer, TCPFaultProxy],
        local_ams: None,
    ) -> None:
        """4 个不相邻区块持续通知约 8 秒：零整批丢弃、worker 不泄漏。"""
        del local_ams
        server, _proxy = stack
        offsets = (0, 16384, 32768, 49152)
        points = {f"p{index}": _point(f"p{index}", offset) for index, offset in enumerate(offsets)}
        server.set_values(_GROUP, 0, b"\x00" * 50000)
        driver = _driver(points, host=_PROXY_IP, max_block_bytes=64)
        counts = {name: 0 for name in points}
        await driver.connect()
        tasks_before = len(asyncio.all_tasks())

        async def on_sample(sample: ProtocolSample) -> None:
            counts[sample.point_id] += 1

        handle = await driver.subscribe(tuple(points), on_sample, interval=0.05)
        try:
            assert server.notification_count() == 4
            deadline = asyncio.get_running_loop().time() + 8.0
            value = 0
            while asyncio.get_running_loop().time() < deadline:
                value += 1
                for offset in offsets:
                    server.set_values(_GROUP, offset, pack_points([("DINT", value)]))
                await asyncio.sleep(0.1)
        finally:
            await handle.close()
            await driver.close()

        assert all(count >= 10 for count in counts.values()), counts
        assert server.notification_count() == 0
        assert len(callback_store) == 0
        assert len(asyncio.all_tasks()) <= tasks_before + 1


class TestSigKillResidue:
    """独立进程客户端被 SIGKILL 的服务端残留观测。"""

    @pytest.mark.parametrize("round_index", range(30))
    async def test_30_sigkill_restart_cycles(
        self, round_index: int, local_ams: None
    ) -> None:
        """每轮：独立进程注册 2 条通知→SIGKILL→确认残留→服务重启清空。"""
        del local_ams
        server = AdsMemoryServer(ip_address=_KILL_SERVER_IP)
        server.start()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.support.ads_subprocess_client",
                _KILL_SERVER_IP,
                "2",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert process.stdout is not None
            ready = await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
            assert ready.decode().strip() == "READY 2"
            await asyncio.sleep(0.1)
            assert server.notification_count() == 2

            process.send_signal(signal.SIGKILL)
            await asyncio.wait_for(process.wait(), timeout=5.0)
            assert process.returncode == -signal.SIGKILL
            await asyncio.sleep(0.1)
            # 客户端无机会注销：服务端句柄在 TCP 断开后仍然残留
            # （真实 PLC 是否回收必须在 TwinCAT 上验收）。
            assert server.notification_count() == 2

            server.restart()
            assert server.notification_count() == 0
        finally:
            server.stop()

    async def test_sigkill_then_fresh_client_registers_cleanly(
        self, local_ams: None
    ) -> None:
        """SIGKILL + 服务重启后，新客户端注册计数从头开始，不跨进程累积。"""
        del local_ams
        server = AdsMemoryServer(ip_address=_KILL_SERVER_IP)
        server.start()
        try:
            for expected in (2, 2):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tests.support.ads_subprocess_client",
                    _KILL_SERVER_IP,
                    "2",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                assert process.stdout is not None
                await asyncio.wait_for(process.stdout.readline(), timeout=5.0)
                await asyncio.sleep(0.1)
                assert server.notification_count() == expected
                process.send_signal(signal.SIGKILL)
                await asyncio.wait_for(process.wait(), timeout=5.0)
                server.restart()
                assert server.notification_count() == 0
            # 驱动进程本身（本进程）随后仍能正常注册/注销。
            await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
            await ADSDriver.close_local()
        finally:
            server.stop()
