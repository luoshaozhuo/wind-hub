"""ADSDriver × 线性内存 ADS 服务的真实协议集成测试。

服务端 ``AdsMemoryServer`` 运行 pyads ``AdsTestServer`` 的真实 AMS/TCP
帧栈（real_service，非 mock），并在其上提供连续字节内存语义：Sum Read/
Write 按字节偏移寻址、Device Notification 按字节区间注册。本文件覆盖：

- 多类型（REAL/DINT/BOOL/STRING）同一 Sum Read 批次与 Sum Write 回读；
- 单点读写、未映射地址点级错误收敛为 BAD 质量；
- 区块通知真实注册→数据到达→注销的完整生命周期（含服务端注册计数）；
- PLC ADS State RUN/STOP 对 connect/health 的影响；
- 服务端重启后新会话可重新连接。

注意：通知数据面经 pyads 进程内 ``callback_store`` 触发（注册/注销命令
本身走真实 TCP）。跨进程客户端的通知投递只能在真实 TwinCAT 上验收，
见交付报告硬件验收清单。
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

from core.application.protocol_contract import ProtocolSample, ProtocolWrite, Quality
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


def _index_point(
    point_id: str,
    offset: int,
    data_type: str,
    *,
    size: int | None = None,
    access: PointAccess = PointAccess.READ_WRITE,
) -> Point:
    ext: dict[str, str | int] = {
        "data_type": data_type,
        "index_group": _GROUP,
        "index_offset": offset,
    }
    if size is not None:
        ext["size"] = size
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=access,
        ext=ext,
    )


@pytest.fixture
async def memory_server() -> AsyncIterator[AdsMemoryServer]:
    server = AdsMemoryServer(ip_address="127.0.0.41")
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


def _driver(points: dict[str, Point], *, ip: str) -> ADSDriver:
    return ADSDriver(
        ConnectionEndpoint(ip, 48898),
        PointTable(PointTableId("mem"), Protocol("ads"), points),
        {"target_net_id": _NET_ID, "timeout": 3.0, "twincat_version": "3"},
    )


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


pytestmark = [pytest.mark.ads, pytest.mark.real_service]


class TestRealReadsWrites:
    async def test_multi_type_sum_read(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        del local_ams
        # REAL 4 + DINT 4 + BOOL 1 + STRING 8 = 17 字节连续内存。
        memory = struct.pack("<fi?", 21.5, -7, True) + b"wind\x00\x00\x00\x00"
        memory_server.set_values(_GROUP, 0, memory)
        points = {
            "temp": _index_point("temp", 0, "REAL", access=PointAccess.READ),
            "speed": _index_point("speed", 4, "DINT"),
            "flag": _index_point("flag", 8, "BOOL"),
            "label": _index_point("label", 9, "STRING", size=8),
        }
        driver = _driver(points, ip=memory_server.ip_address)
        await driver.connect()
        try:
            samples = await driver.read_many(("temp", "speed", "flag", "label"))
            by_id = {sample.point_id: sample for sample in samples}
            assert by_id["temp"].value == pytest.approx(21.5)
            assert by_id["speed"].value == -7
            assert by_id["flag"].value is True
            assert by_id["label"].value == "wind"
            assert all(sample.quality is Quality.GOOD for sample in samples)
        finally:
            await driver.close()

    async def test_sum_write_then_server_memory_readback(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        del local_ams
        points = {
            "a": _index_point("a", 0, "REAL"),
            "b": _index_point("b", 4, "REAL"),
        }
        driver = _driver(points, ip=memory_server.ip_address)
        await driver.connect()
        try:
            results = await driver.write_many(
                (ProtocolWrite("a", 1.5), ProtocolWrite("b", -2.5))
            )
            assert [result.outcome.name for result in results] == [
                "CONFIRMED_SUCCESS",
                "CONFIRMED_SUCCESS",
            ]
            assert memory_server.handler.read_memory(_GROUP, 0, 8) == pack_points(
                [("REAL", 1.5), ("REAL", -2.5)]
            )
        finally:
            await driver.close()

    async def test_single_read_write_and_bad_quality_on_unmapped_address(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 42)]))
        points = {
            "known": _index_point("known", 0, "DINT"),
            "hole": _index_point("hole", 128, "DINT"),  # 服务端从未写入的空洞地址
        }
        driver = _driver(points, ip=memory_server.ip_address)
        await driver.connect()
        try:
            sample = await driver.read_one("known")
            assert sample.value == 42
            assert sample.quality is Quality.GOOD

            result = await driver.write_one(ProtocolWrite("known", 7))
            assert result.outcome.name == "CONFIRMED_SUCCESS"

            # 点级 ADS 错误（1808）在 Sum Read 中收敛为 BAD 样本，不抛传输错误。
            batch = await driver.read_many(("known", "hole"))
            assert batch[0].quality is Quality.GOOD
            assert batch[1].quality is Quality.BAD
            assert batch[1].value is None
            assert driver.is_open()
        finally:
            await driver.close()


class TestRealBlockNotification:
    async def test_notification_lifecycle_registers_data_and_deregisters(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        del local_ams
        memory_server.set_values(_GROUP, 0, pack_points([("REAL", 0.0), ("REAL", 0.0)]))
        points = {
            "a": _index_point("a", 0, "REAL"),
            "b": _index_point("b", 4, "REAL"),
        }
        driver = _driver(points, ip=memory_server.ip_address)
        await driver.connect()
        received: dict[str, float] = {}
        arrived = asyncio.Event()

        async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
            for sample in samples:
                received[sample.point_id] = float(sample.value)  # type: ignore[arg-type]  # 测点为 REAL
                if {"a", "b"} <= received.keys():
                    arrived.set()

        try:
            handle = await driver.subscribe(("a", "b"), on_sample, interval=0.05)
            # 相邻同组点合并为一个区块通知注册（服务端可观测）。
            await _wait_for(lambda: memory_server.notification_count() == 1)

            memory_server.set_values(_GROUP, 0, pack_points([("REAL", 3.5), ("REAL", -8.0)]))
            await asyncio.wait_for(arrived.wait(), timeout=3.0)
            assert received == {"a": 3.5, "b": -8.0}

            await handle.close()
            await _wait_for(lambda: memory_server.notification_count() == 0)

            # 注销后服务端再写内存，客户端不得再收到回调。
            received.clear()
            memory_server.set_values(_GROUP, 0, pack_points([("REAL", 99.0), ("REAL", 99.0)]))
            await asyncio.sleep(0.2)
            assert received == {}
        finally:
            await driver.close()


class TestRealAdsState:
    async def test_connect_rejects_stop_then_succeeds_after_run(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        del local_ams
        points = {"a": _index_point("a", 0, "DINT")}
        memory_server.set_running(False)

        driver = _driver(points, ip=memory_server.ip_address)
        with pytest.raises(Exception, match="connect"):
            await driver.connect()
        assert driver.is_open() is False

        memory_server.set_running(True)
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 11)]))
        try:
            await driver.connect()
            assert driver.is_open()
            assert (await driver.read_one("a")).value == 11

            # 运行中切 STOP：连接仍在但 health 反映非 RUN（与 TCP 断开严格区分）。
            memory_server.set_running(False)
            health = await driver.health()
            assert health.healthy is False
            assert "STOP" in (health.message or "")
        finally:
            await driver.close()

    async def test_server_restart_allows_fresh_session(
        self, memory_server: AdsMemoryServer, local_ams: None
    ) -> None:
        del local_ams
        points = {"a": _index_point("a", 0, "DINT")}
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 5)]))
        driver = _driver(points, ip=memory_server.ip_address)
        await driver.connect()
        assert (await driver.read_one("a")).value == 5
        await driver.close()

        # 模拟 PLC 重启：监听同一地址，内存与通知注册表清空。
        memory_server.restart()
        memory_server.set_values(_GROUP, 0, pack_points([("DINT", 6)]))

        driver2 = _driver(points, ip=memory_server.ip_address)
        try:
            await driver2.connect()
            assert (await driver2.read_one("a")).value == 6
        finally:
            await driver2.close()
