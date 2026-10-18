"""L1：批量写入的点表快照一致性、大批量并发热更新与 connect/close 压力。

- write_many 在途期间点表热更新：本批次按取得快照时的映射执行，
  请求地址不被热更新改变；后续批次才使用新映射；
- 大批量写入与反复热更新并发：结果顺序/数量稳定，不抛异常；
- 连续 connect/close 压力：每轮连接精确关闭一次，无残留 owner Task。
"""

from __future__ import annotations

import asyncio
import struct
import threading
from typing import Any

import pytest
from pyads.constants import ADSIGRP_SUMUP_WRITE  # type: ignore[import-untyped]

from core.application.protocol_contract import ProtocolWrite, WriteOutcome
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
from core.infrastructure.protocol.ads.driver import _AdsSession
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.ads]

_GROUP = 0x4020


def _point(point_id: str, offset: int) -> Point:
    return Point(
        point_id=point_id,
        business_point_id=BusinessPointId(point_id),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "DINT", "index_group": _GROUP, "index_offset": offset},
    )


def _table(n: int, *, offset_step: int = 4) -> PointTable:
    return PointTable(
        PointTableId("batch"),
        Protocol("ads"),
        {f"p{i}": _point(f"p{i}", i * offset_step) for i in range(n)},
    )


def _driver(table: PointTable) -> ADSDriver:
    return ADSDriver(
        ConnectionEndpoint("192.0.2.30", 851),
        table,
        {"target_net_id": "192.0.2.30.1.1", "timeout": 2.0, "twincat_version": "3"},
    )


class SumWriteConnection:
    """记录每次 Sum Write 请求内全部偏移；首个请求可门控阻塞。"""

    def __init__(self, *, gate_first: bool = False) -> None:
        self.is_open = True
        self.close_calls = 0
        self.requests: list[list[int]] = []
        self._lock = threading.Lock()
        self._gate_first = gate_first
        self.entered = threading.Event()
        self.release = threading.Event()

    def read_write(
        self,
        index_group: int,
        count: int,
        read_datatype: object,
        value: object,
        write_datatype: object,
        *,
        check_length: bool,
    ) -> bytes:
        del read_datatype, write_datatype, check_length
        assert index_group == ADSIGRP_SUMUP_WRITE
        raw = bytes(value)  # type: ignore[arg-type]
        offsets = [struct.unpack_from("<III", raw, i * 12)[1] for i in range(count)]
        with self._lock:
            first = not self.requests and self._gate_first
            self.requests.append(offsets)
        if first:
            self.entered.set()
            if not self.release.wait(timeout=5.0):
                raise AssertionError("gated sum write never released")
        return b"\x00" * (4 * count)

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False

    def notification(self, datatype: Any) -> Any:
        del datatype
        return lambda func: func


def _attach(driver: ADSDriver, conn: SumWriteConnection) -> _AdsSession:
    session = _AdsSession(conn)
    driver._session = session  # noqa: SLF001
    driver._connected = True  # noqa: SLF001
    return session


async def _wait(event: threading.Event, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if event.wait(0.01):
            return
        await asyncio.sleep(0)
    raise AssertionError("event not set within timeout")


async def test_write_many_uses_point_snapshot_taken_before_hot_reload() -> None:
    table = _table(50)
    driver = _driver(table)
    conn = SumWriteConnection(gate_first=True)
    _attach(driver, conn)

    writes = [ProtocolWrite(f"p{i}", i) for i in range(50)]
    batch = asyncio.create_task(driver.write_many(writes))
    await _wait(conn.entered)

    # 在途期间热更新：偏移全部换成另一套（直接地址点允许在线更新）。
    driver.update_point_table(_table(50, offset_step=8))
    conn.release.set()

    results = await batch
    assert len(results) == 50
    assert all(result.outcome == WriteOutcome.CONFIRMED_SUCCESS for result in results)
    # 本批次请求使用的是更新前的 4 字节步长快照。
    assert conn.requests[0] == [i * 4 for i in range(50)]

    # 之后的批次按新映射执行。
    await driver.write_many([ProtocolWrite("p0", 1)])
    assert conn.requests[-1] == [0]


async def test_large_batch_concurrent_with_point_table_updates() -> None:
    table = _table(1000)
    driver = _driver(table)
    conn = SumWriteConnection()
    _attach(driver, conn)

    tables = [_table(1000, offset_step=4), _table(1000, offset_step=8)]
    stop = asyncio.Event()

    async def reload_loop() -> None:
        index = 0
        while not stop.is_set():
            driver.update_point_table(tables[index % 2])
            index += 1
            await asyncio.sleep(0)

    reloader = asyncio.create_task(reload_loop())
    try:
        batches = await asyncio.gather(
            *(
                driver.write_many([ProtocolWrite(f"p{i}", i) for i in range(1000)])
                for _ in range(5)
            )
        )
    finally:
        stop.set()
        await reloader

    for results in batches:
        assert len(results) == 1000
        assert [result.point_id for result in results] == [f"p{i}" for i in range(1000)]
        assert all(result.outcome == WriteOutcome.CONFIRMED_SUCCESS for result in results)

    await driver.close()
    assert conn.close_calls == 1


class FastConnection:
    """connect/close 压力用的全即时假连接。"""

    def __init__(self) -> None:
        self.is_open = False
        self.close_calls = 0

    def open(self) -> None:
        self.is_open = True

    def set_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def read_state(self) -> tuple[int, int]:
        return (5, 0)

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False

    def notification(self, datatype: Any) -> Any:
        del datatype
        return lambda func: func


async def test_repeated_connect_close_cycles_reclaim_everything() -> None:
    driver = _driver(_table(4))
    conns: list[FastConnection] = []

    def factory(*_args: object, **_kwargs: object) -> FastConnection:
        conn = FastConnection()
        conns.append(conn)
        return conn

    driver._new_connection = factory  # type: ignore[method-assign]

    for _ in range(100):
        await driver.connect()
        assert driver.is_open()
        await driver.close()
        assert not driver.is_open()

    assert len(conns) == 100
    assert all(conn.close_calls == 1 for conn in conns)
    # 无残留会话、owner 与关闭任务。
    assert driver._session is None  # noqa: SLF001
    leftover = [
        task
        for task in asyncio.all_tasks()
        if (task.get_name() or "").startswith("ads-") and not task.done()
    ]
    assert leftover == []
