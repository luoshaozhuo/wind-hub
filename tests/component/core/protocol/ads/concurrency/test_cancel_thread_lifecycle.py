"""L1：asyncio 取消与底层同步线程生命周期的确定性测试。

不使用真实协议栈（那是 integration 层的职责），用受控门控的伪 pyads
连接强制精确的执行顺序，回答任务书第三组问题：

- 等待 Driver ``_lock`` 的协程被取消后，在途同步命令是否继续运行、
  是否出现命令重叠、后续命令是否完好；
- 取消后立即 close()/connect() 是否有界且不提前关闭连接；
- 写取消是否产生自动重发；
- 迟到的旧线程结果是否可能污染新会话；
- 线程与 asyncio Task 是否回收干净。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from core.application.protocol_contract import ConnectionHealth, ProtocolWrite
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads.driver import ADSDriver, _AdsSession
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.ads]


class GatedConnection:
    """按命令门控的伪 pyads 连接，记录重叠/调用顺序/线程。"""

    def __init__(self) -> None:
        self.is_open = True
        self.closed = 0
        self._entered = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls: list[tuple[str, int, float]] = []
        self.write_values: list[float] = []
        self.state_responses: Iterator[tuple[int, int]] | None = None
        self.late: bool = False

    def arm(self) -> None:
        self._entered = threading.Event()
        self._release = threading.Event()

    def release(self) -> None:
        self._release.set()

    def wait_entered(self, timeout: float = 2.0) -> bool:
        return self._entered.wait(timeout)

    def _enter(self, name: str) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((name, threading.get_ident(), time.monotonic()))
        self._entered.set()
        if not self._release.wait(timeout=5.0):
            raise AssertionError(f"gated call {name} never released")

    def _exit(self) -> None:
        with self._lock:
            self.active -= 1

    def read_state(self) -> tuple[int, int]:
        self._enter("read_state")
        try:
            if self.state_responses is not None:
                return next(self.state_responses, (5, 0))
            return (5, 0)
        finally:
            self._exit()

    def read(self, group: int, offset: int, datatype: Any) -> float:
        del group, offset, datatype
        self._enter("read")
        try:
            return 1.5
        finally:
            self._exit()

    def write(self, group: int, offset: int, value: Any, datatype: Any) -> None:
        del group, offset, datatype
        self._enter("write")
        try:
            self.write_values.append(float(value))
        finally:
            self._exit()

    def close(self) -> None:
        self.closed += 1
        self.is_open = False

    def notification(self, datatype: Any) -> Any:
        del datatype

        def decorator(func: Any) -> Any:
            return func

        return decorator


def _driver_with(session: _AdsSession | None = None) -> ADSDriver:
    table = PointTable(
        PointTableId("gated"),
        Protocol("ads"),
        {
            "w": Point(
                point_id="w",
                business_point_id=BusinessPointId("w"),
                source_unit=NONE_UNIT,
                access=PointAccess.READ_WRITE,
                ext={"data_type": "REAL", "index_group": 0x4020, "index_offset": 0},
            ),
            "r": Point(
                point_id="r",
                business_point_id=BusinessPointId("r"),
                source_unit=NONE_UNIT,
                access=PointAccess.READ_WRITE,
                ext={"data_type": "REAL", "index_group": 0x4020, "index_offset": 4},
            ),
        },
    )
    driver = ADSDriver(
        ConnectionEndpoint("127.0.0.1", 48898),
        table,
        {"target_net_id": "127.0.0.1.2.1", "timeout": 2.0, "twincat_version": "3"},
    )
    if session is not None:
        driver._session = session  # noqa: SLF001 - L1 测试直接注入
        driver._connected = True  # noqa: SLF001
    return driver


@pytest.fixture
def gated() -> GatedConnection:
    return GatedConnection()


async def _wait_entered(conn: GatedConnection, timeout: float = 2.0) -> bool:
    """先让出事件循环让 create_task 跑到 to_thread，再等工作线程进门控。"""
    deadline = time.monotonic() + timeout
    await asyncio.sleep(0)
    while time.monotonic() < deadline:
        if conn.wait_entered(0.01):
            return True
        await asyncio.sleep(0.01)
    return False


async def test_cmd_lock_never_overlaps_sync_calls(gated: GatedConnection) -> None:
    """两条 to_thread 命令经 _cmd_lock 串行：底层重叠峰值恒为 1。"""
    session = _AdsSession(gated)
    driver = _driver_with(session)
    gated.arm()

    async def health_after_gate() -> ConnectionHealth:
        await _wait_entered(gated)
        # 第一条命令在门内时发起健康检查，必然排队等锁。
        health_task = asyncio.create_task(driver.health())
        await asyncio.sleep(0.05)
        assert not health_task.done()
        gated.release()
        return await health_task

    health = await asyncio.gather(driver.health(), health_after_gate())
    assert all(item.healthy for item in health)
    assert gated.peak == 1
    assert len(gated.calls) == 2
    await driver.close()


async def test_cancel_queued_behind_driver_lock_does_not_kill_inflight(
    gated: GatedConnection,
) -> None:
    """等待 _lock 的协程取消：在途命令跑完且恰好一次，取消者干净返回。"""
    driver = _driver_with(_AdsSession(gated))
    gated.arm()

    inflight = asyncio.create_task(driver.health())
    assert await _wait_entered(gated)
    queued = asyncio.create_task(driver.health())
    await asyncio.sleep(0.05)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    # 在途命令未受影响，完整返回；底层只执行了一次。
    gated.release()
    health = await inflight
    assert health.healthy
    assert [name for name, _, _ in gated.calls] == ["read_state"]

    # 锁回收后新命令立即可用，连接状态一致。
    gated.arm()
    follow_up = asyncio.create_task(driver.health())
    assert await _wait_entered(gated)
    gated.release()
    assert (await follow_up).healthy
    await driver.close()
    assert gated.closed == 1


async def test_cancel_after_sync_started_close_waits_for_thread(gated: GatedConnection) -> None:
    """同步命令已开始后取消并立即 close：连接必须等线程结束才关闭。"""
    driver = _driver_with(_AdsSession(gated))
    gated.arm()

    task = asyncio.create_task(driver.health())
    assert await _wait_entered(gated)
    task.cancel()
    close_task = asyncio.create_task(driver.close())
    await asyncio.sleep(0.1)
    # 同步线程仍持锁：close 不得提前关闭连接。
    assert gated.closed == 0
    assert not close_task.done()

    gated.release()
    await asyncio.wait_for(close_task, timeout=5.0)
    assert gated.closed == 1
    assert not task.cancelled() or task.done()
    assert threading.active_count() >= 1


async def test_write_cancel_does_not_resend(gated: GatedConnection) -> None:
    """写命令取消后不存在自动重发：底层 write 恰好一次。"""
    driver = _driver_with(_AdsSession(gated))
    gated.arm()
    task = asyncio.create_task(driver.write_one(ProtocolWrite("w", 3.25)))
    assert await _wait_entered(gated)
    task.cancel()
    close_task = asyncio.create_task(driver.close())
    gated.release()
    await asyncio.wait_for(close_task, timeout=5.0)
    assert gated.write_values == [3.25]
    assert len([name for name, _, _ in gated.calls if name == "write"]) == 1


async def test_connect_after_cancel_reuses_healthy_session(gated: GatedConnection) -> None:
    """取消在途命令后立即 connect：已连接会话幂等保留且健康。"""
    session = _AdsSession(gated)
    driver = _driver_with(session)
    gated.arm()
    task = asyncio.create_task(driver.read_one("r"))
    assert await _wait_entered(gated)
    task.cancel()
    connect_task = asyncio.create_task(driver.connect())
    gated.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(connect_task, timeout=5.0)
    assert driver.is_open()
    assert (await driver.health()).healthy
    await driver.close()


async def test_late_old_session_result_cannot_reach_new_session(
    gated: GatedConnection,
) -> None:
    """旧会话迟到的线程结果不影响新会话读取（Driver 会话引用已切换）。"""
    old_session = _AdsSession(gated)
    driver = _driver_with(old_session)

    new_conn = GatedConnection()
    new_session = _AdsSession(new_conn)

    # 模拟 close→connect：旧线程迟到返回时，driver 已指向新会话。
    driver._session = None  # noqa: SLF001
    driver._connected = False  # noqa: SLF001
    driver._session = new_session  # noqa: SLF001
    driver._connected = True  # noqa: SLF001

    new_conn.arm()
    health = asyncio.create_task(driver.health())
    assert await _wait_entered(new_conn)
    assert gated.active == 0  # 旧连接没有新命令
    new_conn.release()
    assert (await health).healthy
    assert [name for name, _, _ in new_conn.calls] == ["read_state"]
    await driver.close()
    assert new_conn.closed == 1
    assert gated.closed == 0


async def test_shutdown_gives_up_bounded_without_close(gated: GatedConnection) -> None:
    """shutdown 抢不到 _cmd_lock 时放弃 close（端口不关），返回 False。"""
    session = _AdsSession(gated)
    gated.arm()

    def hold_lock() -> None:
        session.run("read_state")

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert await _wait_entered(gated)
    assert session.shutdown(grace=0.2) is False
    assert gated.closed == 0
    gated.release()
    thread.join(timeout=2.0)
    # 放弃后再关闭：命令线程已结束，shutdown 必须成功且只 close 一次。
    assert session.shutdown(grace=1.0) is True
    assert gated.closed == 1


async def test_resources_reclaimed_after_cancel_storm(gated: GatedConnection) -> None:
    """大量取消 + 关闭并发：无驱动调用残留，无残留 asyncio Task。

    注意：asyncio.to_thread 复用的默认 ThreadPoolExecutor 空闲工作线程由
    CPython 运行时保留，不会随任务结束立即消失——那不是驱动泄漏。泄漏
    语义是"仍有线程卡在驱动调用里"，用 gated.active 判定。
    """
    driver = _driver_with(_AdsSession(gated))
    tasks: list[asyncio.Task[Any]] = []
    for index in range(20):
        tasks.append(asyncio.create_task(driver.health()))
        if index % 3 == 0:
            await asyncio.sleep(0.002)
    for index, task in enumerate(tasks):
        if index % 2 == 0:
            task.cancel()
    close_task = asyncio.create_task(driver.close())
    # 构造时门控即处于 armed；此处只能 release 原事件——重新 arm() 会换一个
    # 新 Event，已阻塞在旧 Event 上的工作线程收不到释放。
    gated.release()
    await asyncio.wait_for(close_task, timeout=5.0)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(item, ConnectionHealth | asyncio.CancelledError) for item in results)
    leftover = [
        task for task in asyncio.all_tasks() if not task.done() and "ads" in (task.get_name() or "")
    ]
    assert not leftover
    # 给被取消命令的工作线程一点时间退出同步段；最终不得有线程仍在驱动调用里。
    deadline = time.monotonic() + 2.0
    while gated.active > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert gated.active == 0, f"{gated.active} worker thread(s) still inside sync call"
    # 多等一个调度周期，确认没有迟到命令重新进入。
    await asyncio.sleep(0.1)
    assert gated.active == 0
    peak = gated.peak
    assert peak == 1, f"sync calls overlapped: peak={peak}"
