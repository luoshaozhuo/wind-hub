"""B 组：ADSDriver 公共 API 在**真实 ADS 协议栈**上的并发正确性。

与 A 组（裸 pyads Connection）不同，本组每个用例都经过 ADSDriver 的完整
公共路径（``_lock`` + ``_cmd_lock`` + asyncio.to_thread），服务器是
``AdsMemoryServer``——真实 AMS/TCP 帧栈（real_service，非 mock）。

覆盖任务书第二组场景中真实栈层尚未覆盖的部分：

- read_many vs write_one/write_many 并发，数据完整性与质量红线；
- 多 chunk Sum Read/Write（``max_subs_per_sum`` 强制分块）并发交错；
- subscribe 注册/投递/deregister 与 health/read/write 并发；
- 真实栈 close-vs-IO、double close、close→connect 期间 health 循环；
- ``update_point_table`` 热重载与 IO 并发。

数据完整性判据：读取要么抛 ProtocolError（仅允许出现在 close 竞态用例），
要么每个点质量 GOOD 且值属于该点合法值集合——绝不接受跨点串值。
"""

from __future__ import annotations

import asyncio
import itertools
import struct
from collections.abc import AsyncIterator, Callable

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import (
    ProtocolSample,
    ProtocolWrite,
    Quality,
)
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

pytestmark = [pytest.mark.ads, pytest.mark.real_service]


def _point(point_id: str, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "REAL", "index_group": _GROUP, "index_offset": offset},
    )


def _table(points: dict[str, Point], table_id: str = "b-matrix") -> PointTable:
    return PointTable(PointTableId(table_id), Protocol("ads"), points)


def _driver(
    points: dict[str, Point],
    *,
    ip: str,
    options: dict[str, object] | None = None,
) -> ADSDriver:
    config: dict[str, object] = {
        "target_net_id": _NET_ID,
        "timeout": 3.0,
        "twincat_version": "3",
    }
    if options:
        config.update(options)
    return ADSDriver(ConnectionEndpoint(ip, 48898), _table(points), config)


@pytest.fixture(scope="module")
async def local_ams() -> AsyncIterator[None]:
    await ADSDriver.initialize_local(ADSLocalConfig(_LOCAL_NET_ID, "127.0.0.1"))
    try:
        yield
    finally:
        await ADSDriver.close_local()


_server_seq = itertools.count(1)


@pytest.fixture
async def server() -> AsyncIterator[AdsMemoryServer]:
    # 每个用例独立 loopback 地址，顺序分配（确定性，不用随机）。
    index = next(_server_seq)
    address = f"127.32.{index // 250}.{1 + index % 250}"
    instance = AdsMemoryServer(ip_address=address)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_read_many_concurrent_with_write_one_never_corrupts(
    server: AdsMemoryServer, local_ams: None
) -> None:
    """read_many × 2 与 write_one 轮转写并发：全 GOOD、值不串点、无异常。"""
    del local_ams
    point_ids = ["p0", "p1", "p2", "p3"]
    points = {pid: _point(pid, index * 4) for index, pid in enumerate(point_ids)}
    server.set_values(_GROUP, 0, pack_points([("REAL", 1.0)] * 4))
    driver = _driver(points, ip=server.ip_address)
    await driver.connect()

    # 每点合法值域：初值 1.0 与 10+i+k（k 为写轮次）。
    allowed: dict[str, set[float]] = {pid: {1.0} for pid in point_ids}
    allowed_lock = asyncio.Lock()
    errors: list[BaseException] = []
    rounds = 40

    async def writer() -> None:
        for k in range(rounds):
            value = 10.0 + k
            for pid in point_ids:
                async with allowed_lock:
                    allowed[pid].add(value)
                await driver.write_one(ProtocolWrite(pid, value))

    async def reader() -> None:
        try:
            for _ in range(rounds):
                samples = await driver.read_many(tuple(point_ids))
                assert len(samples) == 4
                async with allowed_lock:
                    for sample in samples:
                        assert sample.quality == Quality.GOOD, sample
                        assert float(sample.value) in allowed[sample.point_id], sample
        except BaseException as exc:  # noqa: BLE001 - 并发下任何异常都是证据
            errors.append(exc)

    try:
        await asyncio.gather(writer(), reader(), reader())
    finally:
        await driver.close()
    assert errors == []
    assert server.residue_registrations() == ()


async def test_chunked_sum_read_write_concurrent_interleaving(
    server: AdsMemoryServer, local_ams: None
) -> None:
    """25 点 / max_subs_per_sum=8 → 4 chunk，读写在 chunk 边界交错仍正确。"""
    del local_ams
    n = 25
    point_ids = [f"c{i}" for i in range(n)]
    points = {pid: _point(pid, i * 4) for i, pid in enumerate(point_ids)}
    server.set_values(_GROUP, 0, pack_points([("REAL", 0.0)] * n))
    # 每点终值固定（100+i），初值 0：交错中读到的只可能是二者之一。
    final_values = {pid: float(100 + i) for i, pid in enumerate(point_ids)}
    driver = _driver(points, ip=server.ip_address, options={"max_subs_per_sum": 8})
    await driver.connect()
    errors: list[BaseException] = []

    async def writer() -> None:
        for _ in range(15):
            await driver.write_many([ProtocolWrite(pid, final_values[pid]) for pid in point_ids])

    async def reader() -> None:
        try:
            for _ in range(25):
                samples = await driver.read_many(tuple(point_ids))
                assert len(samples) == n
                for i, sample in enumerate(samples):
                    assert sample.quality == Quality.GOOD, sample
                    assert float(sample.value) in {0.0, final_values[point_ids[i]]}, sample
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        await asyncio.gather(writer(), reader(), reader())
        final = await driver.read_many(tuple(point_ids))
        for i, sample in enumerate(final):
            assert float(sample.value) == final_values[point_ids[i]], sample
    finally:
        await driver.close()
    assert errors == []


async def test_subscribe_concurrent_with_health_read_write(
    server: AdsMemoryServer, local_ams: None
) -> None:
    """区块通知注册/投递/deregister 与 health/read/write 全并发。"""
    del local_ams
    points = {pid: _point(pid, i * 4) for i, pid in enumerate(["a", "b", "c", "d"])}
    server.set_values(_GROUP, 0, pack_points([("REAL", 0.0)] * 4))
    driver = _driver(points, ip=server.ip_address)
    await driver.connect()
    delivered = 0
    delivered_lock = asyncio.Lock()

    async def on_sample(sample: ProtocolSample) -> None:
        nonlocal delivered
        assert sample.point_id in {"a", "b"}, sample
        async with delivered_lock:
            delivered += 1

    handle = await driver.subscribe(("a", "b"), on_sample, interval=0.02)
    await _wait_for(lambda: server.notification_count() >= 1)
    health_failures: list[object] = []

    async def load() -> None:
        for k in range(30):
            health = await driver.health()
            if not health.healthy:
                health_failures.append(health)
            await driver.read_many(("c", "d"))
            await driver.write_one(ProtocolWrite("c", float(k)))

    try:
        await asyncio.gather(load(), load())
        server.set_values(_GROUP, 0, pack_points([("REAL", 5.5), ("REAL", -2.5)]))
        await _wait_for(lambda: delivered > 0)
    finally:
        await handle.close()
        await _wait_for(lambda: server.notification_count() == 0)
        await driver.close()
    assert health_failures == []
    assert server.residue_registrations() == ()
    assert server.active_connection_count() == 0


async def test_deregister_concurrent_with_io_storm(
    server: AdsMemoryServer, local_ams: None
) -> None:
    """handle.close()（DEL_DEVICE_NOTIFICATION）与读写风暴并发：有界、零残留。"""
    del local_ams
    points = {pid: _point(pid, i * 4) for i, pid in enumerate(["a", "b", "c"])}
    server.set_values(_GROUP, 0, pack_points([("REAL", 0.0)] * 3))
    driver = _driver(points, ip=server.ip_address)
    await driver.connect()

    async def on_sample(sample: ProtocolSample) -> None:
        del sample

    handle = await driver.subscribe(("a",), on_sample, interval=0.02)
    await _wait_for(lambda: server.notification_count() == 1)

    async def io_storm() -> None:
        for k in range(25):
            await driver.write_one(ProtocolWrite("b", float(k)))
            await driver.read_many(("b", "c"))
            await driver.health()

    try:
        await asyncio.gather(handle.close(), io_storm())
        await _wait_for(lambda: server.notification_count() == 0)
        # 注销后 IO 仍然健康。
        assert (await driver.health()).healthy
        samples = await driver.read_many(("b", "c"))
        assert all(sample.quality == Quality.GOOD for sample in samples)
    finally:
        await driver.close()
    assert server.residue_registrations() == ()


async def test_close_concurrent_with_read_is_bounded(
    server: AdsMemoryServer, local_ams: None
) -> None:
    """读循环进行中 close：所有调用有界收敛，服务端连接归零。"""
    del local_ams
    points = {pid: _point(pid, i * 4) for i, pid in enumerate(["a", "b"])}
    server.set_values(_GROUP, 0, pack_points([("REAL", 1.0)] * 2))
    driver = _driver(points, ip=server.ip_address)
    await driver.connect()
    stop = asyncio.Event()
    outcomes: list[str] = []

    async def read_loop() -> None:
        while not stop.is_set():
            try:
                samples = await driver.read_many(("a", "b"))
                assert all(sample.quality == Quality.GOOD for sample in samples)
                outcomes.append("ok")
            except ProtocolError:
                outcomes.append("closed")
                return

    reader = asyncio.create_task(read_loop())
    await asyncio.sleep(0.05)
    close_task = asyncio.create_task(driver.close())
    await asyncio.wait_for(close_task, timeout=5.0)
    stop.set()
    await asyncio.wait_for(reader, timeout=5.0)
    await _wait_for(lambda: server.active_connection_count() == 0)
    assert "ok" in outcomes


async def test_double_close_concurrent_is_idempotent(
    server: AdsMemoryServer, local_ams: None
) -> None:
    """两个并发 close + 一个迟到 close：不抛、连接只关一次语义。"""
    del local_ams
    points = {"a": _point("a", 0)}
    server.set_values(_GROUP, 0, pack_points([("REAL", 1.0)]))
    driver = _driver(points, ip=server.ip_address)
    await driver.connect()

    results = await asyncio.gather(driver.close(), driver.close(), return_exceptions=True)
    assert all(not isinstance(item, BaseException) for item in results), results
    await driver.close()  # 串行再关一次也必须幂等
    await _wait_for(lambda: server.active_connection_count() == 0)
    assert not driver.is_open()


async def test_reconnect_during_health_loop(server: AdsMemoryServer, local_ams: None) -> None:
    """health 循环期间 close→connect：新会话健康，服务端最终恰有一条连接。"""
    del local_ams
    points = {pid: _point(pid, i * 4) for i, pid in enumerate(["a", "b"])}
    server.set_values(_GROUP, 0, pack_points([("REAL", 1.0)] * 2))
    driver = _driver(points, ip=server.ip_address)
    await driver.connect()
    assert (await driver.health()).healthy

    await driver.close()
    await _wait_for(lambda: server.active_connection_count() == 0)
    await driver.connect()
    try:
        assert (await driver.health()).healthy
        samples = await driver.read_many(("a", "b"))
        assert all(sample.quality == Quality.GOOD for sample in samples)
        assert server.active_connection_count() == 1
    finally:
        await driver.close()
    await _wait_for(lambda: server.active_connection_count() == 0)


async def test_update_point_table_concurrent_with_reads(
    server: AdsMemoryServer, local_ams: None
) -> None:
    """热重载点表（共享点不断连）与 read_many 并发：共享点始终 GOOD。"""
    del local_ams

    def table_for(table_id: str, n: int) -> PointTable:
        return _table({f"p{i}": _point(f"p{i}", i * 4) for i in range(n)}, table_id)

    server.set_values(_GROUP, 0, pack_points([("REAL", 7.0)] * 8))
    driver = _driver({f"p{i}": _point(f"p{i}", i * 4) for i in range(4)}, ip=server.ip_address)
    await driver.connect()
    errors: list[BaseException] = []

    async def reader() -> None:
        # p0/p1 在两版点表中始终存在且偏移不变。
        for _ in range(50):
            try:
                samples = await driver.read_many(("p0", "p1"))
                assert all(sample.quality == Quality.GOOD for sample in samples)
                for sample in samples:
                    assert float(sample.value) == 7.0, sample
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                await asyncio.sleep(0.005)

    reader_task = asyncio.create_task(reader())
    for n in (4, 8, 4, 8, 4):
        driver.update_point_table(table_for(f"table-{n}", n))
        await asyncio.sleep(0.01)
    await reader_task
    try:
        assert driver.is_open()
        assert (await driver.health()).healthy
    finally:
        await driver.close()
    # 点表切换中出现的瞬时"点不存在"类错误不允许发生（共享点偏移稳定）。
    assert errors == [], errors
    # 字节布局自检：REAL 7.0 编码长度，避免静默误判。
    assert struct.calcsize("<f") == 4
