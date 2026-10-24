"""connection.close() 超过异步预算后的 Session 线程所有权测试。

这里阻塞的不是在途业务命令，而是
``connection.close()`` 本身——它已经在工作线程内持有 ``_cmd_lock`` 执行，
asyncio 等待预算却先耗尽。用 threading.Event 精确控制底层 close 的起止，
验证 T1、T3、T4：

- T1 异步等待超时后底层 close 线程仍被可靠追踪，迟到完成被观察且只关一次；
- T3 等待关闭的调用方被取消，底层关闭的生命周期所有权不丢失；
- T4 并发/重复关闭收敛到同一条底层 close，所有调用方得到一致结果。

所有用例自带硬超时；fake 的阻塞释放也带 5s 自检，失败不会挂死测试进程。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core.infrastructure.protocol.ads.session import _AdsSession

pytestmark = [pytest.mark.ads]

# aclose 管线总预算 = grace + 2s；小 grace 用例需承受约 2s 的确定性等待。
_FAST_GRACE = 0.02
_PIPELINE_BUDGET = _FAST_GRACE + 2.0


def _wait_until_set(event: threading.Event, timeout: float) -> bool:
    """按单调时钟守住硬期限，避免底层定时等待提前返回造成假超时。"""
    deadline = time.monotonic() + timeout
    while not event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        event.wait(remaining)
    return True


def test_close_gate_tolerates_early_wait_return(monkeypatch: pytest.MonkeyPatch) -> None:
    event = threading.Event()
    calls = 0

    def early_wait(timeout: float | None = None) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            event.set()
        return event.is_set()

    monkeypatch.setattr(event, "wait", early_wait)
    assert _wait_until_set(event, 0.1)
    assert calls == 2


def test_close_gate_keeps_deadline_after_early_wait_return(monkeypatch: pytest.MonkeyPatch) -> None:
    event = threading.Event()
    ticks = iter([0.0, 0.0, 0.04, 0.1])
    waits: list[float | None] = []

    def early_wait(timeout: float | None = None) -> bool:
        waits.append(timeout)
        return False

    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(event, "wait", early_wait)
    assert not _wait_until_set(event, 0.1)
    assert waits == pytest.approx([0.1, 0.06])


class CloseBlockingConnection:
    """close() 可被线程级 Event 阻塞的 pyads 连接 fake，记录真实起止时刻。"""

    def __init__(self, name: str, order: list[str], *, fail_close: bool = False) -> None:
        self.name = name
        self._order = order
        self.is_open = True
        self.close_calls = 0
        self.close_started = threading.Event()
        self.close_gate = threading.Event()
        self.close_finished = threading.Event()
        self.close_begin = 0.0
        self.close_end = 0.0
        self.close_thread_id: int | None = None
        self.fail_close = fail_close
        self._lock = threading.Lock()

    def _log(self, event: str) -> None:
        with self._lock:
            self._order.append(f"{self.name}:{event}")

    def open(self) -> None:
        self._log("open")
        self.is_open = True

    def set_timeout(self, timeout_ms: int) -> None:
        del timeout_ms

    def read_state(self) -> tuple[int, int]:
        return (5, 0)

    def read(self, group: int, offset: int, datatype: object) -> int:
        del group, datatype
        return offset + 11

    def close(self) -> None:
        with self._lock:
            self.close_calls += 1
            self.close_thread_id = threading.get_ident()
            self.close_begin = time.monotonic()
        self._log("close-start")
        self.close_started.set()
        if not _wait_until_set(self.close_gate, timeout=5.0):
            raise AssertionError(f"{self.name}.close() was never released")
        with self._lock:
            self.close_end = time.monotonic()
            self.is_open = False
            self.close_finished.set()
        self._log("close-end")
        if self.fail_close:
            raise RuntimeError(f"{self.name} close boom")


async def _wait_event(event: threading.Event, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if event.wait(0.01):
            return
        await asyncio.sleep(0)
    raise AssertionError("threading event was not set within timeout")


async def test_t1_close_thread_remains_tracked_after_async_budget_expired() -> None:
    """T1：异步预算耗尽 ≠ 底层关闭结束；迟到成功必须被观察且状态一致。"""
    order: list[str] = []
    connection = CloseBlockingConnection("old", order)
    connection.open()
    session = _AdsSession(connection)

    started = asyncio.get_running_loop().time()
    result = await asyncio.wait_for(session.aclose(_FAST_GRACE), timeout=4.0)
    returned_at = asyncio.get_running_loop().time() - started
    assert result is False
    # 等待确实在管线预算处有界返回，而不是提前或无限等待。
    assert _PIPELINE_BUDGET - 0.1 <= returned_at <= _PIPELINE_BUDGET + 0.5

    # 底层 close 线程仍在运行，且仍是同一个物理关闭动作。
    assert connection.close_started.is_set()
    assert not connection.close_finished.is_set()
    assert connection.close_calls == 1
    # 门已归还（后续命令看到 _closing 自行拒绝），但 _cmd_lock 仍被关闭线程持有。
    assert not session._gate.locked()  # noqa: SLF001
    assert not session._cmd_lock.acquire(timeout=0.05)  # noqa: SLF001
    # 物理关闭尚未收敛：有强引用持有的受追踪 owner，而非无人等待的游离 future。
    assert session.physical_close_pending
    owner = session._shutdown_task  # noqa: SLF001
    assert owner is not None and not owner.done()

    # 迟到完成：结果被观察、只关一次、锁与线程全部回收。
    connection.close_gate.set()
    assert await asyncio.wait_for(session.wait_close_settled(2.0), timeout=3.0) is True
    assert connection.close_finished.wait(timeout=2.0)
    assert owner.done() and owner.exception() is None
    assert connection.close_calls == 1
    assert session._cmd_lock.acquire(timeout=1.0)  # noqa: SLF001
    session._cmd_lock.release()  # noqa: SLF001
    assert not session.physical_close_pending
    assert order == ["old:open", "old:close-start", "old:close-end"]


async def test_t1_late_close_failure_is_observed_without_second_close() -> None:
    """T1 变体：迟到的 connection.close() 抛异常：观察为失败收敛，不补第二次 close。"""
    connection = CloseBlockingConnection("old", [], fail_close=True)
    connection.open()
    session = _AdsSession(connection)

    assert await asyncio.wait_for(session.aclose(_FAST_GRACE), timeout=4.0) is False
    assert session.physical_close_pending
    # 放行阻塞的 close 线程：它走到终点后抛出 close 自身故障。
    connection.close_gate.set()
    # 物理线程已结束（即便以异常结束）即视为收敛；不重发 close。
    assert await asyncio.wait_for(session.wait_close_settled(2.0), timeout=3.0) is True
    assert connection.close_finished.wait(timeout=2.0)
    assert connection.close_calls == 1
    assert not session.physical_close_pending


async def test_t3_caller_cancel_keeps_close_ownership_and_converges() -> None:
    """T3：等待 aclose 的调用方被取消，底层关闭继续受追踪并可收敛为成功。"""
    connection = CloseBlockingConnection("old", [])
    connection.open()
    session = _AdsSession(connection)

    closer = asyncio.create_task(session.aclose(1.0))
    await _wait_event(connection.close_started)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer

    # 管线与底层线程都没有被取消：物理关闭仍在途且受追踪。
    assert connection.close_calls == 1
    assert not connection.close_finished.is_set()
    assert session.physical_close_pending

    connection.close_gate.set()
    # 取消者之后的再次 await 收敛到同一管线；底层及时成功，结果为 True。
    assert await asyncio.wait_for(session.aclose(1.0), timeout=2.0) is True
    assert connection.close_finished.wait(timeout=2.0)
    assert connection.close_calls == 1
    assert not session.physical_close_pending


async def test_t4_concurrent_and_repeated_close_share_single_physical_close() -> None:
    """T4：超时返回后并发/重复 aclose 得到一致 False，底层 close 绝不重复提交。"""
    connection = CloseBlockingConnection("old", [])
    connection.open()
    session = _AdsSession(connection)

    results = await asyncio.wait_for(
        asyncio.gather(
            session.aclose(_FAST_GRACE),
            session.aclose(_FAST_GRACE),
            session.aclose(_FAST_GRACE),
        ),
        timeout=4.0,
    )
    assert results == [False, False, False]
    # 超时后再次关闭：复用同一管线结果，立即返回且不启动第二次物理关闭。
    assert await asyncio.wait_for(session.aclose(_FAST_GRACE), timeout=1.0) is False
    assert connection.close_calls == 1
    assert session.physical_close_pending

    connection.close_gate.set()
    assert await asyncio.wait_for(session.wait_close_settled(2.0), timeout=3.0) is True
    assert connection.close_calls == 1
    assert connection.close_finished.wait(timeout=2.0)
    # 收敛后重复关闭仍是幂等结果，不补发 close。
    assert await asyncio.wait_for(session.aclose(_FAST_GRACE), timeout=1.0) is True
    assert connection.close_calls == 1
