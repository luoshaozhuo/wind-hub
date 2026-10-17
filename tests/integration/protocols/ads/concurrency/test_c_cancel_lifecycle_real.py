"""C 组：asyncio 取消 × 真实 pyads/AdsLib 线程的生命周期测试。

L1（tests/component/.../test_cancel_thread_lifecycle.py）用门控假连接验证
驱动内部不变量；本组在**真实协议栈**上验证相同语义并取证：

- 取消在途读：线上恰好一条 READ 命令，命令跑完但结果被丢弃，后续命令
  健康，服务端同步调用重叠峰值恒为 1（Q5：取消不会制造重叠同步命令）；
- 取消在途写：无自动重发，线上恰好一条 WRITE，内存为单次写入值；
- 取消在途 health 后立即再 health：两条 READSTATE 严格串行；
- 取消订阅注册后 close：迟到 handle 被清理，服务端零注册残留；
- 取消风暴 + close：全部有界收敛，服务端连接归零。

延迟注入用 ControlledAdsMemoryHandler.set_delay；取消时机用 arrival_count
（命令已进入服务器、正在延迟窗口）确定，不使用随机 sleep。
"""

from __future__ import annotations

import asyncio
import itertools
import struct
from collections.abc import AsyncIterator, Callable

import pytest
from pyads import constants  # type: ignore[import-untyped]

from core.application.protocol_contract import ProtocolSample, ProtocolWrite
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
from tests.support.ads_controlled_server import ControlledAdsMemoryServer
from tests.support.ads_memory_server import pack_points
from tests.support.units import NONE_UNIT

_GROUP = 0x4020
_NET_ID = "127.0.0.1.2.1"
_LOCAL_NET_ID = "127.0.0.1.1.1"

pytestmark = [pytest.mark.ads, pytest.mark.real_service, pytest.mark.slow]

_seq = itertools.count(1)
_READ = constants.ADSCOMMAND_READ
_WRITE = constants.ADSCOMMAND_WRITE
_READ_STATE = constants.ADSCOMMAND_READSTATE
_ADD_NOTE = constants.ADSCOMMAND_ADDDEVICENOTE


def _point(point_id: str, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "REAL", "index_group": _GROUP, "index_offset": offset},
    )


async def _wait_arrivals(control: object, command: int, count: int, timeout: float = 3.0) -> None:
    """等到 command 的入口到达次数达到 count（命令已在途）。"""

    def arrived() -> bool:
        return control.arrival_count(command) >= count  # type: ignore[attr-defined]

    await _wait_predicate(arrived, timeout=timeout)


async def _wait_predicate(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"condition not met within {timeout}s")


class RealRig:
    def __init__(self, n_points: int = 4) -> None:
        index = next(_seq)
        address = f"127.35.{index // 250}.{1 + index % 250}"
        points = {f"p{i}": _point(f"p{i}", i * 4) for i in range(n_points)}
        self.point_ids = tuple(points)
        self.server = ControlledAdsMemoryServer(ip_address=address)
        self.driver = ADSDriver(
            ConnectionEndpoint(address, 48898),
            PointTable(PointTableId("c"), Protocol("ads"), points),
            {"target_net_id": _NET_ID, "timeout": 5.0, "twincat_version": "3"},
        )

    async def start(self) -> None:
        self.server.start()
        await self.driver.connect()
        # connect 自身发过一条 READSTATE 健康校验；实验计数从零开始。
        self.server.control.reset_telemetry()

    async def stop(self) -> None:
        await self.driver.close()
        self.server.stop()

    @property
    def control(self) -> object:
        return self.server.control

    def init_memory(self, values: list[tuple[str, float]] | None = None) -> None:
        if values is None:
            values = [("REAL", 1.5)] * len(self.point_ids)
        self.server.set_values(_GROUP, 0, pack_points(values))


@pytest.fixture(scope="module")
async def local_ams() -> AsyncIterator[None]:
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    try:
        yield
    finally:
        await ADSDriver.close_local()


async def test_c01_cancel_inflight_read_single_wire_command(
    local_ams: None,
) -> None:
    """取消在途 read_one：线上一条 READ；并发 health 排队不重叠。"""
    del local_ams
    rig = RealRig()
    await rig.start()
    rig.init_memory()
    rig.control.set_delay(_READ, 0.5)  # type: ignore[attr-defined]
    try:
        read_task = asyncio.create_task(rig.driver.read_one("p0"))
        await _wait_arrivals(rig.control, _READ, 1)

        read_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read_task

        # 立即发起 health：旧同步线程仍在服务器延迟窗口，health 必须排队，
        # 服务端两种命令的重叠峰值都应为 1。
        health_task = asyncio.create_task(rig.driver.health())
        # 给 health 协程时间进入 _lock 等待。
        await asyncio.sleep(0.05)
        assert not health_task.done(), "health overtook the inflight read"
        health = await asyncio.wait_for(health_task, timeout=3.0)
        assert health.healthy

        assert rig.control.arrival_count(_READ) == 1  # type: ignore[attr-defined]
        assert rig.control.peak_active(_READ) == 1  # type: ignore[attr-defined]
        assert rig.control.peak_active(_READ_STATE) == 1  # type: ignore[attr-defined]

        # 在途命令结束后，读路径完好，值正确（无串扰）。
        rig.control.set_delay(_READ, 0.0)  # type: ignore[attr-defined]
        sample = await rig.driver.read_one("p0")
        assert sample.quality.name == "GOOD"
        assert float(sample.value) == 1.5
    finally:
        await rig.stop()


async def test_c02_cancel_inflight_write_no_resend(local_ams: None) -> None:
    """取消在途 write_one：线上恰好一条 WRITE，无重发。"""
    del local_ams
    rig = RealRig()
    await rig.start()
    rig.init_memory([("REAL", 0.0), ("REAL", 0.0)])
    rig.control.set_delay(_WRITE, 0.5)  # type: ignore[attr-defined]
    try:
        write_task = asyncio.create_task(rig.driver.write_one(ProtocolWrite("p1", 9.25)))
        await _wait_arrivals(rig.control, _WRITE, 1)
        write_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await write_task

        # 等待在途命令的服务器延迟窗口结束并完成内存写入。
        await asyncio.sleep(0.7)
        assert rig.control.arrival_count(_WRITE) == 1  # type: ignore[attr-defined]

        # 服务端内存恰为单次写入值：取消未触发第二条写。
        memory = rig.server.handler.read_memory(_GROUP, 4, 4)
        assert memory is not None
        assert struct.unpack("<f", memory)[0] == 9.25

        rig.control.set_delay(_WRITE, 0.0)  # type: ignore[attr-defined]
        follow_up = await rig.driver.write_one(ProtocolWrite("p1", 3.5))
        assert follow_up.outcome.name == "CONFIRMED_SUCCESS"
        assert rig.control.arrival_count(_WRITE) == 2  # type: ignore[attr-defined]
    finally:
        await rig.stop()


async def test_c03_cancel_then_reissue_health_stays_serial(local_ams: None) -> None:
    """在途 health 被取消后立刻再发：两条 READSTATE 严格串行、都可观测。"""
    del local_ams
    rig = RealRig()
    await rig.start()
    rig.control.set_delay(_READ_STATE, 0.5)  # type: ignore[attr-defined]
    try:
        first = asyncio.create_task(rig.driver.health())
        await _wait_arrivals(rig.control, _READ_STATE, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(rig.driver.health())
        # 第一条还在延迟窗口时，第二条不得已到达服务器。
        await asyncio.sleep(0.1)
        assert rig.control.arrival_count(_READ_STATE) == 1  # type: ignore[attr-defined]
        assert await asyncio.wait_for(second, timeout=3.0)
        assert rig.control.arrival_count(_READ_STATE) == 2  # type: ignore[attr-defined]
        assert rig.control.peak_active(_READ_STATE) == 1  # type: ignore[attr-defined]
    finally:
        await rig.stop()


async def test_c04_cancel_subscribe_registration_close_drains_handle(
    local_ams: None,
) -> None:
    """订阅注册在途时取消并 close：迟到 handle 被注销，服务端零残留。"""
    del local_ams
    rig = RealRig()
    await rig.start()
    rig.init_memory()
    rig.control.set_delay(_ADD_NOTE, 0.5)  # type: ignore[attr-defined]

    async def on_sample(sample: ProtocolSample) -> None:
        del sample

    subscribe_task = asyncio.create_task(rig.driver.subscribe(("p0",), on_sample, interval=0.05))
    try:
        await _wait_arrivals(rig.control, _ADD_NOTE, 1)
        subscribe_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await subscribe_task

        # close 必须等在途注册命令结束，并把迟到 handle 注销掉。
        await asyncio.wait_for(rig.driver.close(), timeout=5.0)

        def zero_registrations() -> bool:
            return len(rig.server.handler.residue_registrations()) == 0

        await _wait_predicate(zero_registrations, timeout=3.0)
        assert rig.server.handler.notification_count() == 0
    finally:
        if rig.driver.is_open():
            await rig.driver.close()
        rig.server.stop()


async def test_c05_cancel_storm_then_close_bounded(local_ams: None) -> None:
    """24 个混合任务半数取消后 close：有界收敛、无连接泄漏。"""
    del local_ams
    rig = RealRig(n_points=8)
    await rig.start()
    rig.init_memory()
    tasks: list[asyncio.Task[object]] = []

    async def job(seq: int) -> object:
        if seq % 3 == 0:
            return await rig.driver.read_many(rig.point_ids)
        if seq % 3 == 1:
            return await rig.driver.write_one(ProtocolWrite("p0", float(seq)))
        return await rig.driver.health()

    for index in range(24):
        tasks.append(asyncio.create_task(job(index)))
        if index % 4 == 0:
            await asyncio.sleep(0.002)

    for index, task in enumerate(tasks):
        if index % 2 == 0:
            task.cancel()

    try:
        await asyncio.wait_for(rig.driver.close(), timeout=5.0)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            # 取消之外的任何异常都不允许出现（无假失败、无帧串扰）。
            if isinstance(item, BaseException):
                assert isinstance(item, asyncio.CancelledError), repr(item)
        # 服务端全部命令串行（按命令峰值 1）。
        for command in (_READ, _WRITE, _READ_STATE):
            assert rig.control.peak_active(command) <= 1  # type: ignore[attr-defined]

        def connections_gone() -> bool:
            return rig.server.handler.active_connection_count() == 0

        await _wait_predicate(connections_gone, timeout=3.0)
    finally:
        rig.server.stop()
