"""单连接命令通道并发安全回归测试。

pyads 在 Linux 上每个 ``Connection`` 是一条同步请求/响应 TCP 通道，
不支持多线程并发收发。Watchdog 的 ``read_state`` 曾经绕过 Driver 的
IO 锁，与业务读写并发时 AMS 帧串扰，表现为 ``ADSError(-1)``：
业务命令假失败、健康检查假不健康（进而触发误重连）。

本模块在线性内存 ADS 测试服务（真实 AMS/TCP 协议栈）上强制并发，
是该缺陷的回归测试，而非 mock 行为验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from core.application.protocol_contract import ProtocolWrite
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

_IP = "127.0.0.31"
_GROUP = 0x4020
_NET_ID = "127.0.0.1.2.1"
_LOCAL_NET_ID = "127.0.0.1.1.1"

pytestmark = [pytest.mark.ads, pytest.mark.real_service]


def _index_point(point_id: str, offset: int, data_type: str = "REAL") -> Point:
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


@pytest.fixture
async def memory_driver() -> AsyncIterator[ADSDriver]:
    server = AdsMemoryServer(ip_address=_IP)
    server.start()
    server.set_values(_GROUP, 0, pack_points([("REAL", 1.0), ("REAL", 2.0)]))
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    table = PointTable(
        PointTableId("race"),
        Protocol("ads"),
        {"w": _index_point("w", 0), "r": _index_point("r", 4)},
    )
    driver = ADSDriver(
        ConnectionEndpoint(_IP, 48898),
        table,
        {"target_net_id": _NET_ID, "timeout": 3.0, "twincat_version": "3"},
    )
    try:
        await driver.connect()
        yield driver
    finally:
        await driver.close()
        await ADSDriver.close_local()
        server.stop()


async def test_health_concurrent_with_writes_never_corrupts_commands(
    memory_driver: ADSDriver,
) -> None:
    """并发 health 与 write 不得出现帧串扰假失败。"""
    iterations = 100
    write_failures: list[Exception] = []
    unhealthy: list[str] = []

    async def write_loop() -> None:
        for index in range(iterations):
            try:
                result = await memory_driver.write_one(ProtocolWrite("w", float(index)))
                if result.outcome.name != "CONFIRMED_SUCCESS":
                    write_failures.append(AssertionError(result.message))
            except Exception as exc:  # noqa: BLE001 - 记录而非中断并发对端
                write_failures.append(exc)

    async def health_loop() -> None:
        for _ in range(iterations):
            health = await memory_driver.health()
            if not health.healthy:
                unhealthy.append(health.message)

    await asyncio.gather(write_loop(), health_loop())

    assert not unhealthy, unhealthy[:5]
    assert not write_failures, [str(exc) for exc in write_failures[:5]]


async def test_health_concurrent_with_sum_reads_never_corrupts_commands(
    memory_driver: ADSDriver,
) -> None:
    """并发 health 与 Sum Read：读取不得抛错，health 不得假失败。"""
    iterations = 100
    errors: list[Exception] = []
    unhealthy: list[str] = []

    async def read_loop() -> None:
        for _ in range(iterations):
            try:
                samples = await memory_driver.read_many(("w", "r"))
                bad = [sample for sample in samples if sample.quality.name != "GOOD"]
                if bad:
                    errors.append(AssertionError(f"bad samples: {bad}"))
            except Exception as exc:  # noqa: BLE001 - 记录而非中断并发对端
                errors.append(exc)

    async def health_loop() -> None:
        for _ in range(iterations):
            health = await memory_driver.health()
            if not health.healthy:
                unhealthy.append(health.message)

    await asyncio.gather(read_loop(), health_loop())

    assert not unhealthy, unhealthy[:5]
    assert not errors, [str(exc) for exc in errors[:5]]
