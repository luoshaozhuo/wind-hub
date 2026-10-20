"""L1：命令 owner 门模型、统计真值与关闭语义的确定性测试。

覆盖任务书第三/四/五节：

- ``_CommandStats`` 成功/异常/1861 超时/门后取消/迟到完成/关闭拒绝的计数；
- 调用方取消后，同步线程结束前 owner 不放门，后续命令不进线程池空等
  ``_cmd_lock``；
- owner 异常在无等待者时被回收（不产生 "Task exception was never
  retrieved"）；
- ``shutdown`` 在 ``connection.close()`` 失败时返回 False；
- ``aclose`` 的总预算覆盖等门阶段且放弃后绝不补关；
- ``aclose`` 调用方取消不中断关闭管线（幂等收敛）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pyads
import pytest

from core.application.errors import ConfigError, ProtocolError
from core.infrastructure.protocol.ads.session import _AdsSession

pytestmark = [pytest.mark.ads]


class GateConnection:
    """可阻塞、可注入异常的伪连接，记录进入 run 的工作线程。"""

    def __init__(
        self,
        *,
        fail: dict[str, BaseException] | None = None,
        fail_on_release: dict[str, BaseException] | None = None,
    ) -> None:
        self.is_open = True
        self.closed = 0
        self.close_raises: BaseException | None = None
        # fail：命令一进入即抛（不走门控）；fail_on_release：门控放行后抛。
        self._fail = fail or {}
        self._fail_on_release = fail_on_release or {}
        self._entered = threading.Event()
        self._release = threading.Event()
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.run_entries = 0

    def arm(self) -> None:
        self._entered = threading.Event()
        self._release = threading.Event()

    def release(self) -> None:
        self._release.set()

    def wait_entered(self, timeout: float = 2.0) -> bool:
        return self._entered.wait(timeout)

    def _block(self, name: str) -> None:
        immediate = self._fail.get(name)
        if immediate is not None:
            with self._lock:
                self.run_entries += 1
            raise immediate
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.run_entries += 1
        self._entered.set()
        if not self._release.wait(timeout=5.0):
            raise AssertionError(f"gated call {name} never released")
        with self._lock:
            self.active -= 1
        failure = self._fail_on_release.get(name)
        if failure is not None:
            raise failure

    def read_state(self) -> tuple[int, int]:
        self._block("read_state")
        return (5, 0)

    def read(self, group: int, offset: int, datatype: Any) -> int:
        del group, offset, datatype
        self._block("read")
        return 1

    def close(self) -> None:
        self.closed += 1
        if self.close_raises is not None:
            raise self.close_raises
        self.is_open = False

    def notification(self, datatype: Any) -> Any:
        del datatype
        return lambda func: func


async def _wait_entered(conn: GateConnection, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    await asyncio.sleep(0)
    while time.monotonic() < deadline:
        if conn.wait_entered(0.01):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("worker never entered gated call")


async def test_success_stats_count_and_execution_time() -> None:
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    task = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)
    hold = 0.1
    await asyncio.sleep(hold)
    conn.release()
    assert await task == (5, 0)

    snap = session.stats.snapshot()
    assert snap["commands"] == {"read_state": 1}
    assert snap["execution"]["count"] == 1
    assert snap["execution"]["total_seconds"] >= hold * 0.9
    assert snap["gate_wait"]["count"] == 1
    assert snap["timeouts"] == 0
    assert await session.aclose(1.0) is True
    assert conn.closed == 1


async def test_ads_error_is_recorded_as_execution_not_timeout() -> None:
    conn = GateConnection(fail={"read_state": pyads.ADSError(1808)})
    session = _AdsSession(conn)
    with pytest.raises(pyads.ADSError):
        await session.execute("read_state")
    snap = session.stats.snapshot()
    assert snap["execution"]["count"] == 1
    assert snap["timeouts"] == 0
    assert await session.aclose(1.0) is True


async def test_ads_1861_timeout_is_recorded() -> None:
    conn = GateConnection(fail={"read_state": pyads.ADSError(1861)})
    session = _AdsSession(conn)
    with pytest.raises(pyads.ADSError):
        await session.execute("read_state")
    snap = session.stats.snapshot()
    assert snap["execution"]["count"] == 1
    assert snap["timeouts"] == 1
    assert await session.aclose(1.0) is True


async def test_cancel_while_queued_never_enters_thread() -> None:
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    inflight = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)
    queued = asyncio.create_task(session.execute("read"))
    await asyncio.sleep(0.05)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    snap = session.stats.snapshot()
    assert snap["cancelled_while_queued"] == 1
    # 排队取消不计数命令与执行，只有在途的 read_state 过门。
    assert snap["commands"] == {"read_state": 1}
    assert conn.run_entries == 1

    conn.release()
    await inflight
    assert session.stats.snapshot()["execution"]["count"] == 1
    assert await session.aclose(1.0) is True


async def test_cancel_after_thread_start_records_true_completion_late() -> None:
    """调用方取消后 owner 仍跑完：统计以线程真实结束为准，且只记一次。"""
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    task = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 线程未结束前：执行统计不得提前入账。
    await asyncio.sleep(0.1)
    assert session.stats.snapshot()["execution"]["count"] == 0
    assert conn.closed == 0

    hold_more = 0.1
    await asyncio.sleep(hold_more)
    conn.release()
    # 等 owner 完成（门释放后新命令能取得即说明 owner 已记账退出）。
    conn.arm()
    follow = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)
    conn.release()
    assert await follow == (5, 0)

    snap = session.stats.snapshot()
    assert snap["execution"]["count"] == 2
    # 第一次执行的耗时包含取消后的"迟到"等待，而不是取消时刻。
    assert snap["execution"]["max_seconds"] >= 0.15
    assert await session.aclose(1.0) is True
    assert conn.closed == 1


async def test_command_after_closing_flag_is_rejected() -> None:
    conn = GateConnection()
    session = _AdsSession(conn)
    session._closing = True  # noqa: SLF001 - 直接构造关闭起点
    with pytest.raises(ProtocolError, match="closing"):
        await session.execute("read_state")
    snap = session.stats.snapshot()
    assert snap["rejected_after_closing"] == 1
    assert snap["execution"]["count"] == 0
    assert conn.run_entries == 0


async def test_gate_wait_and_execution_times_are_distinct() -> None:
    """排队等待 ≈ 在途命令持门时长；排队者自身执行耗时接近 0。

    A 门控持门 H（执行≈H、等待≈0），B/C 在 A 之后排队（等待≈H、
    执行≈0）：等待合计 ≈2H，而执行合计 ≈H。
    """
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    first = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)
    hold = 0.15
    queued = [
        asyncio.create_task(session.execute("read_state")),
        asyncio.create_task(session.execute("read_state")),
    ]
    await asyncio.sleep(hold)
    conn.release()
    assert await first == (5, 0)
    assert await asyncio.gather(*queued) == [(5, 0), (5, 0)]
    snap = session.stats.snapshot()
    assert snap["gate_wait"]["count"] == 3
    assert snap["execution"]["count"] == 3
    assert snap["gate_wait"]["total_seconds"] >= hold * 1.8
    # 只有 A 的一次真实持门，执行合计 ≈ H；等待合计 ≈ 2H。
    assert snap["execution"]["total_seconds"] < hold * 1.5
    assert await session.aclose(1.0) is True


async def test_cancelled_caller_keeps_gate_until_sync_thread_finishes() -> None:
    """Q3：A 取消后 B/C/D 不得进入工作线程空等 _cmd_lock。"""
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)

    task_a = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)
    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a

    followers = [asyncio.create_task(session.execute("read_state")) for _ in range(3)]
    await asyncio.sleep(0.15)
    # owner 仍持门：B/C/D 连 to_thread 都不应进入。
    assert conn.run_entries == 1
    assert conn.peak == 1
    assert all(not task.done() for task in followers)

    conn.release()
    results = await asyncio.gather(*followers)
    assert results == [(5, 0)] * 3
    assert conn.run_entries == 4
    assert conn.peak == 1
    assert await session.aclose(1.0) is True
    assert conn.closed == 1


async def test_owner_exception_is_reclaimed_without_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待者已取消而命令后失败：owner 异常必须被消费，循环无未观察异常。"""
    conn = GateConnection(fail_on_release={"read": pyads.ADSError(1808)})
    conn.arm()
    session = _AdsSession(conn)
    messages: list[str] = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        messages.append(context.get("message", ""))

    loop.set_exception_handler(handler)
    try:
        task = asyncio.create_task(session.execute("read", 0, 0, None))
        await _wait_entered(conn)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        conn.release()  # read 在工作线程内抛 ADSError
        await asyncio.sleep(0.1)
        assert session._owners == set()  # noqa: SLF001
        assert messages == []
    finally:
        loop.set_exception_handler(old_handler)
    assert await session.aclose(1.0) is True


async def test_shutdown_close_failure_returns_false_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = GateConnection()
    conn.close_raises = OSError("adsDelRoute refused")
    session = _AdsSession(conn, label="192.0.2.10:851")

    assert session.shutdown(grace=1.0) is False
    assert conn.closed == 1  # close 确实尝试过
    assert session.stats.snapshot()["shutdown_abandoned"] == 0  # 由管线计数
    assert "ADS connection close failed" in caplog.text
    assert "192.0.2.10:851" in caplog.text
    # 失败后仍可重试关闭（第二次 fake 不再抛）。
    conn.close_raises = None
    assert session.shutdown(grace=1.0) is True
    assert conn.closed == 2


async def test_aclose_returns_false_when_close_raises() -> None:
    conn = GateConnection()
    conn.close_raises = OSError("boom")
    session = _AdsSession(conn)
    assert await session.aclose(0.05) is False
    snap = session.stats.snapshot()
    assert snap["shutdown_abandoned"] == 1
    # 幂等：重复 aclose 收敛到同一管线结果，不谎报成功。
    assert await session.aclose(1.0) is False
    assert conn.closed == 1


async def test_aclose_gate_wait_timeout_abandons_without_closing() -> None:
    """总预算覆盖等门阶段：在途 owner 不结束则不发出 close。"""
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    inflight = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)

    started = time.monotonic()
    closed = await session.aclose(grace=0.02)  # 总预算 ≈ 2.02s
    elapsed = time.monotonic() - started
    assert closed is False
    assert 1.5 < elapsed < 3.0
    assert conn.closed == 0
    snap = session.stats.snapshot()
    assert snap["shutdown_abandoned"] == 1

    # 在途命令结束后端口仍可手动关闭；放弃期间命令未受影响。
    conn.release()
    assert await inflight == (5, 0)
    assert conn.run_entries == 1


async def test_aclose_caller_cancel_does_not_interrupt_shutdown() -> None:
    """取消等待 aclose 的调用方：关闭管线继续，端口最终关闭且只关一次。"""
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    inflight = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)

    closing = asyncio.create_task(session.aclose(1.0))
    await asyncio.sleep(0.05)
    assert not closing.done()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    conn.release()
    assert await inflight == (5, 0)
    # 再次 await 收敛到同一管线：真实结果为关闭成功。
    assert await session.aclose(1.0) is True
    assert conn.closed == 1


async def test_shutdown_lock_wait_failure_returns_false() -> None:
    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    holder = asyncio.create_task(session.execute("read_state"))
    await _wait_entered(conn)
    # 绕过门直接在另一线程等 _cmd_lock：抢不到即 False，且不 close。
    result = await asyncio.to_thread(session.shutdown, 0.05)
    assert result is False
    assert conn.closed == 0
    conn.release()
    assert await holder == (5, 0)
    assert await session.aclose(1.0) is True
    assert conn.closed == 1


async def test_cancel_storm_does_not_saturate_thread_pool_with_lock_waiters() -> None:
    """取消风暴下门后排队命令不占用工作线程：4 容量池里探针始终可跑。"""
    from concurrent.futures import ThreadPoolExecutor

    conn = GateConnection()
    conn.arm()
    session = _AdsSession(conn)
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="probe-pool")
    loop.set_default_executor(pool)
    try:
        leader = asyncio.create_task(session.execute("read_state"))
        await _wait_entered(conn)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

        # 取消后立刻涌入排队命令：它们只能在门后等，不得进入线程池。
        followers = [asyncio.create_task(session.execute("read_state")) for _ in range(6)]
        await asyncio.sleep(0.1)
        assert conn.run_entries == 1

        # 4 个探针占满全部工作线程都能即时完成：池没有被 _cmd_lock 等待者占用。
        async def probe() -> bool:
            return await asyncio.to_thread(lambda: True)

        probes = await asyncio.wait_for(asyncio.gather(*(probe() for _ in range(4))), timeout=1.0)
        assert probes == [True] * 4

        conn.release()
        assert await asyncio.gather(*followers) == [(5, 0)] * 6
        assert conn.peak == 1
        assert conn.run_entries == 7
    finally:
        assert await session.aclose(1.0) is True
        pool.shutdown(wait=True)
        # Python 3.11 set_default_executor 不接受 None；直接复位惰性默认值。
        loop._default_executor = None  # type: ignore[attr-defined]


async def test_config_error_is_not_swallowed_by_stats() -> None:
    """非 ADS 异常同样透传并计入一次执行。"""
    conn = GateConnection(fail={"read": ConfigError("bad type")})
    session = _AdsSession(conn)
    with pytest.raises(ConfigError):
        await session.execute("read", 0, 0, None)
    snap = session.stats.snapshot()
    assert snap["execution"]["count"] == 1
    assert await session.aclose(1.0) is True
