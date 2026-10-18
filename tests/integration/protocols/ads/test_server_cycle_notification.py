"""ADSTRANS_SERVERCYCLE 周期通知语义测试（真实 AMS/TCP 控制面）。

服务端 ``AdsMemoryServer`` 运行 pyads ``AdsTestServer`` 的真实 AMS/TCP
帧栈：ADD_DEVICE_NOTIFICATION 的传输模式 / 周期 / 通知长度、DELETE 以及
RUN/STOP、重启均经真实协议帧交互（real_service，非 mock）。

**数据面边界（必须如实标注）**：受 pyads testserver 架构限制，通知数据
投递走进程内 ``callback_store``，客户端与服务端在同一 OS 进程内。本文件
验证的是「真实 AMS/TCP 注册控制面 + 进程内 Notification Callback 数据面」，
**不是完整的跨进程 ADS Notification 验证**；跨进程投递只能在真实 TwinCAT
硬件上验收。跨进程的句柄残留观测见
``tests/reliability/recovery/test_ads_recovery_leaks.py``。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator

import pytest

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
from tests.support.units import NONE_UNIT

_GROUP = 0x4020
_NET_ID = "127.0.0.1.2.1"
_LOCAL_NET_ID = "127.0.0.1.1.1"
_IP = "127.0.0.42"

pytestmark = [pytest.mark.ads, pytest.mark.real_service]


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
    max_block_bytes: int = 1024,
    net_id: str = _NET_ID,
    ip: str = _IP,
) -> ADSDriver:
    return ADSDriver(
        ConnectionEndpoint(ip, 48898),
        PointTable(PointTableId("cycle"), Protocol("ads"), points),
        {
            # AdsLib 目标路由按 Net ID 进程级共享且无引用计数：同 Net ID 的
            # 并发连接会在任一连接关闭时被连带 adsDelRoute，因此并发连接必须
            # 使用不同 Net ID（见 driver._RouteState）。
            "target_net_id": net_id,
            "timeout": 3.0,
            "twincat_version": "3",
            "max_block_bytes": max_block_bytes,
        },
    )


@pytest.fixture
async def memory_server() -> AsyncIterator[AdsMemoryServer]:
    server = AdsMemoryServer(ip_address=_IP)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
async def local_ams() -> AsyncIterator[None]:
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    try:
        yield
    finally:
        await ADSDriver.close_local()


async def _collect(
    driver: ADSDriver, point_id: str, interval: float
) -> tuple[object, list[int]]:
    """订阅单个 DINT 点，返回 (订阅句柄, 收到的值序列)。"""
    seen: list[int] = []

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            seen.append(int(sample.value))  # type: ignore[arg-type]  # 测点为 DINT

    handle = await driver.subscribe((point_id,), on_sample, interval=interval)
    return handle, seen


class TestServerCycleSemantics:
    async def test_unchanged_value_is_delivered_every_cycle(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """数据保持不变时，SERVERCYCLE 仍按注册周期持续上送。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 11)]))
        driver = _driver({"a": _point("a", 0)})
        await driver.connect()
        try:
            handle, seen = await _collect(driver, "a", interval=0.05)
            try:
                # 不做任何后续写入：0.4s 内也应收到多个相同值。
                await asyncio.sleep(0.4)
            finally:
                await handle.close()
            assert len(seen) >= 3, seen
            assert set(seen) == {11}
        finally:
            await driver.close()

    async def test_changed_value_arrives_at_next_cycle(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """数据变化后无需再写，下一周期自动推送新值。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 1)]))
        driver = _driver({"a": _point("a", 0)})
        await driver.connect()
        try:
            handle, seen = await _collect(driver, "a", interval=0.05)
            try:
                await asyncio.sleep(0.12)
                memory_server.set_values(_GROUP, 0, pack_points([("DINT", 9)]))
                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    if seen and seen[-1] == 9:
                        break
                    await asyncio.sleep(0.01)
                assert seen[-1] == 9
                assert 1 in seen
            finally:
                await handle.close()
        finally:
            await driver.close()

    async def test_independent_cycle_periods_on_separate_handles(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """两个独立连接/句柄按各自周期投递，不相互干扰。"""
        del local_ams
        # 两个连接分别落在独立的服务器/Net ID 上，使用不同 IndexOffset。
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 1)]))
        driver_fast = _driver({"a": _point("a", 0)})
        # 独立连接必须使用不同 Net ID：同 Net ID 并发连接共享 AdsLib 进程
        # 路由，任一关闭都会连带删除另一连接依赖的路由（driver 在建链准入期
        # 直接拒绝这种配置）。第二个连接使用独立服务器 IP；AdsMemoryServer
        # 按存活连接的目标 Net ID 过滤进程内通知投递，多实例互不串扰。
        slow_ip = "127.0.0.43"
        slow_server = AdsMemoryServer(ip_address=slow_ip)
        slow_server.start()
        slow_server.set_values(_GROUP, 8192, pack_points([("DINT", 2)]))
        driver_slow = _driver({"b": _point("b", 8192)}, net_id=f"{slow_ip}.2.1", ip=slow_ip)
        try:
            await driver_fast.connect()
            await driver_slow.connect()
            fast: list[int] = []
            slow: list[int] = []

            async def on_fast(samples: tuple[ProtocolSample, ...]) -> None:
                for sample in samples:
                    fast.append(int(sample.value))  # type: ignore[arg-type]

            async def on_slow(samples: tuple[ProtocolSample, ...]) -> None:
                for sample in samples:
                    slow.append(int(sample.value))  # type: ignore[arg-type]

            handle_fast = await driver_fast.subscribe(("a",), on_fast, interval=0.05)
            handle_slow = await driver_slow.subscribe(("b",), on_slow, interval=0.15)
            try:
                await asyncio.sleep(1.6)
            finally:
                await handle_fast.close()
                await handle_slow.close()
            # 宽容差：OS 调度非实时，只断言快慢档明显分层且均持续投递。
            assert len(fast) >= 15, len(fast)
            assert len(slow) >= 4, len(slow)
            ratio = len(fast) / len(slow)
            assert 1.8 <= ratio <= 4.5, (len(fast), len(slow))
        finally:
            await driver_fast.close()
            await driver_slow.close()
            slow_server.stop()

    async def test_cycle_parameter_controls_inter_arrival_time(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """周期参数真实生效：相邻投递间隔围绕注册周期（宽容差）。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 5)]))
        driver = _driver({"a": _point("a", 0)})
        await driver.connect()
        try:
            arrival: list[float] = []

            async def on_sample(_sample: ProtocolSample) -> None:
                arrival.append(time.monotonic())

            handle = await driver.subscribe(("a",), on_sample, interval=0.1)
            try:
                await asyncio.sleep(1.0)
            finally:
                await handle.close()
            deltas = [b - a for a, b in zip(arrival, arrival[1:], strict=False)]
            assert len(deltas) >= 2, arrival
            assert all(0.04 <= delta <= 0.22 for delta in deltas), deltas
        finally:
            await driver.close()

    async def test_deregister_stops_periodic_delivery_immediately(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """注销 Handle 后对应周期通知立即停止。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 1)]))
        memory_server.set_values(_GROUP, 8192, pack_points([("DINT", 2)]))
        driver = _driver(
            {"a": _point("a", 0), "b": _point("b", 8192)}, max_block_bytes=64
        )
        await driver.connect()
        try:
            count_a = 0
            count_b = 0

            async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
                for sample in samples:
                    nonlocal count_a, count_b
                    if sample.point_id == "a":
                        count_a += 1
                    else:
                        count_b += 1

            handle = await driver.subscribe(("a", "b"), on_sample, interval=0.05)
            await asyncio.sleep(0.15)
            assert count_a >= 1
            assert count_b >= 1
            await handle.close()
            assert memory_server.notification_count() == 0
            await asyncio.sleep(0.3)
            stopped_a, stopped_b = count_a, count_b
            await asyncio.sleep(0.3)
            assert count_a == stopped_a
            assert count_b == stopped_b
        finally:
            await driver.close()

    async def test_runtime_stop_pauses_and_run_resumes_cycle(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """Runtime STOP 期间暂停周期投递（TCP 不断）；恢复 RUN 后续传。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 7)]))
        driver = _driver({"a": _point("a", 0)})
        await driver.connect()
        try:
            count = 0

            async def on_sample(_sample: ProtocolSample) -> None:
                nonlocal count
                count += 1

            handle = await driver.subscribe(("a",), on_sample, interval=0.05)
            try:
                await asyncio.sleep(0.2)
                assert count >= 1
                memory_server.set_running(False)
                await asyncio.sleep(0.15)
                paused = count
                # STOP 持续约 8 个周期：不得继续投递，也不补发。
                await asyncio.sleep(0.4)
                assert count == paused, (paused, count)

                memory_server.set_running(True)
                deadline = asyncio.get_running_loop().time() + 2.0
                while asyncio.get_running_loop().time() < deadline:
                    if count > paused:
                        break
                    await asyncio.sleep(0.01)
                assert count > paused
            finally:
                await handle.close()
        finally:
            await driver.close()

    async def test_restart_does_not_deliver_old_handles(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """服务重启后旧 Handle 注册全部消失，新会话注册照常周期投递。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 3)]))
        driver = _driver({"a": _point("a", 0)})
        await driver.connect()
        handle, seen = await _collect(driver, "a", interval=0.05)
        await asyncio.sleep(0.15)
        assert seen
        ident_before = memory_server.scheduler_ident()
        await handle.close()
        await driver.close()

        memory_server.restart()
        assert memory_server.notification_count() == 0
        assert memory_server.allocated_handles() == 0
        # 重启清表后等待超过旧周期：没有任何旧句柄再被投递。
        await asyncio.sleep(0.25)
        assert memory_server.scheduler_ident() == ident_before

        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 4)]))
        driver2 = _driver({"a": _point("a", 0)})
        try:
            await driver2.connect()
            handle2, seen2 = await _collect(driver2, "a", interval=0.05)
            try:
                await asyncio.sleep(0.2)
            finally:
                await handle2.close()
            assert seen2
            assert set(seen2) == {4}
        finally:
            await driver2.close()

    async def test_register_deregister_cycles_leak_no_scheduler_threads(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """50 次注册/注销后调度线程仍是同一个；server.stop 后线程退出。"""
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 0)]))
        driver = _driver({"a": _point("a", 0)})
        await driver.connect()
        threads_start = threading.active_count()
        try:

            async def on_sample(_sample: ProtocolSample) -> None:
                pass

            ident: int | None = None
            for _ in range(50):
                handle = await driver.subscribe(("a",), on_sample, interval=0.05)
                current = memory_server.scheduler_ident()
                assert current is not None
                if ident is None:
                    ident = current
                else:
                    assert current == ident
                assert memory_server.notification_count() == 1
                await handle.close()
                assert memory_server.notification_count() == 0
        finally:
            await driver.close()
        assert threading.active_count() <= threads_start + 2
        memory_server.stop()
        assert memory_server.scheduler_alive() is False

    async def test_many_blocks_schedule_stays_stable(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        """40 个区块同时注册：各区块独立按周期稳定投递，注销后归零。"""
        del local_ams
        offsets = tuple(index * 8192 for index in range(40))
        points = {
            f"p{index}": _point(f"p{index}", offset)
            for index, offset in enumerate(offsets)
        }
        memory_server.set_values(_GROUP, 0, b"\x00" * (40 * 8192))
        driver = _driver(points, max_block_bytes=64)
        await driver.connect()
        counts = {name: 0 for name in points}

        async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
            for sample in samples:
                counts[sample.point_id] += 1

        handle = await driver.subscribe(tuple(points), on_sample, interval=0.1)
        try:
            assert memory_server.notification_count() == 40
            await asyncio.sleep(2.5)
        finally:
            await handle.close()
            await driver.close()
        laggards = sorted(
            (name, count) for name, count in counts.items() if count < 5
        )
        assert not laggards, laggards[:5]
        assert memory_server.notification_count() == 0
