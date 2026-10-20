"""ADSSubscription.close() 生命周期幂等性确定性测试（Component，可控阻塞 fake）。

覆盖任务书 P1：

- 正常关闭 / 重复关闭 / 先 deactivate 后 close；
- 两个并发 close 不重复注销；
- deregister、stop_worker 已单独执行后再 close；
- 单个 Handle 注销失败不影响其余 Handle，失败 Handle 进入 unconfirmed；
- close 等待期间调用方被取消：清理在后台继续完成；
- 注册线程在“服务端可能已注册、客户端尚未拿到 Handle”的窗口内被取消：
  晚到返回的 Handle 必须被接住并注销；注册晚到失败则不得虚构 Handle；
- close 后迟到原生回调不再投递、无 Worker/Handle 残留。
"""

from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import Callable
from typing import Any

import pytest

import core.infrastructure.protocol.ads.subscription as subscription_module
from core.application.protocol_contract import ProtocolSample
from core.infrastructure.protocol.ads.mapping import ADSPoint
from core.infrastructure.protocol.ads.session import _AdsSession
from core.infrastructure.protocol.ads.subscription import ADSSubscription


def _point(name: str, offset: int) -> ADSPoint:
    return ADSPoint(name, 0x4020, offset, "REAL", 4)


class _BlockingSession:
    """注册/注销可按 Handle 阻塞与失败的命令通道 fake。"""

    def __init__(self) -> None:
        self.callbacks: dict[int, Callable[..., None]] = {}
        self.deleted: list[int] = []
        self.delete_attempts: list[int] = []
        self._next = 1
        self.add_started = threading.Event()
        self.add_release = threading.Event()
        self.add_raises: Exception | None = None
        self.del_started: dict[int, threading.Event] = {}
        self.del_release: dict[int, threading.Event] = {}
        self.hold_del: set[int] = set()
        self.fail_del: set[int] = set()
        self._lock = threading.Lock()

    def notification(
        self, datatype: object
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        del datatype
        return lambda callback: callback

    def add_device_notification(
        self,
        address: object,
        attr: object,
        callback: Callable[..., None],
    ) -> tuple[int, None]:
        del address, attr
        self.add_started.set()
        if not self.add_release.wait(timeout=5.0):
            raise TimeoutError("test gate: add_device_notification stuck")
        if self.add_raises is not None:
            raise self.add_raises
        with self._lock:
            handle = self._next
            self._next += 1
            self.del_started[handle] = threading.Event()
            if handle in self.hold_del:
                self.del_release[handle] = threading.Event()
            self.callbacks[handle] = callback
        return (handle, None)

    def del_device_notification(self, handle: int, user_handle: object) -> None:
        del user_handle
        with self._lock:
            self.delete_attempts.append(handle)
            started = self.del_started.get(handle)
            release = self.del_release.get(handle)
        if started is not None:
            started.set()
        if release is not None and not release.wait(timeout=5.0):
            raise TimeoutError("test gate: del_device_notification stuck")
        if handle in self.fail_del:
            raise ConnectionError("deregistration lost")
        with self._lock:
            self.callbacks.pop(handle, None)
            self.deleted.append(handle)

    def run(self, name: str, *args: Any) -> Any:
        return getattr(self, name)(*args)

    async def execute(self, name: str, *args: Any) -> Any:
        # 测试替身：对齐生产 _AdsSession.execute——阻塞命令必须在工作线程，
        # 否则门控会卡住事件循环（本替身不验证 Session 串行化）。
        return await asyncio.to_thread(self.run, name, *args)

    def release_all_del(self) -> None:
        for event in self.del_release.values():
            event.set()

    def fire(self, handle: int, value: float) -> None:
        raw = (struct.Struct("<f").pack)(value)
        self.callbacks[handle](handle, (0x4020, 0), None, raw)


class _Attrib:
    def __init__(
        self, length: int, *, trans_mode: int, cycle_time: float, max_delay: float = 0.0
    ) -> None:
        self.length = length
        self.trans_mode = trans_mode
        self.cycle_time = cycle_time
        self.max_delay = max_delay


class _FakePyads:
    ADSTRANS_SERVERCYCLE = 3
    NotificationAttrib = _Attrib


@pytest.fixture
def fake_pyads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subscription_module, "pyads", _FakePyads)


async def _noop(_samples: tuple[ProtocolSample, ...]) -> None:
    pass


async def _start(session: _BlockingSession, *points: ADSPoint) -> ADSSubscription:
    subscription = ADSSubscription(
        _AdsSession(session),
        points,
        _noop,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    await subscription.start()
    return subscription


async def _wait_event(event: threading.Event, timeout: float = 2.0) -> None:
    """轮询线程事件，期间持续让出事件循环使后台任务推进。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if event.wait(0.01):
            return
        await asyncio.sleep(0)
    raise AssertionError("threading event was not set within timeout")


def _worker_tasks() -> list[asyncio.Task[None]]:
    return [task for task in asyncio.all_tasks() if "ads-subscription-worker" in task.get_name()]


@pytest.mark.asyncio
async def test_normal_close_deletes_handles_and_stops_worker(
    fake_pyads: None,
) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    subscription = await _start(session, _point("a", 0), _point("b", 2000))
    assert set(session.callbacks) == {1, 2}

    await asyncio.wait_for(subscription.close(), timeout=1.0)

    assert session.deleted == [1, 2]
    assert session.callbacks == {}
    assert subscription._worker_task is None
    assert _worker_tasks() == []
    assert subscription.unconfirmed_handles == ()


@pytest.mark.asyncio
async def test_repeated_close_deletes_each_handle_once(fake_pyads: None) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    subscription = await _start(session, _point("a", 0), _point("b", 2000))

    await asyncio.wait_for(subscription.close(), timeout=1.0)
    await asyncio.wait_for(subscription.close(), timeout=1.0)
    await asyncio.wait_for(subscription.close(), timeout=1.0)

    assert session.delete_attempts == [1, 2]
    assert session.deleted == [1, 2]


@pytest.mark.asyncio
async def test_deactivate_before_close_still_deregisters(fake_pyads: None) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    subscription = await _start(session, _point("a", 0))
    delivered = asyncio.Event()

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        if samples:
            delivered.set()

    subscription._callback = on_sample
    subscription.deactivate()  # 只停投递，不代表 Handle/Worker 已清理。

    await asyncio.wait_for(subscription.close(), timeout=1.0)

    assert session.deleted == [1]
    assert subscription._worker_task is None
    assert not delivered.is_set()


@pytest.mark.asyncio
async def test_two_concurrent_closes_deregister_once(fake_pyads: None) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    subscription = await _start(session, _point("a", 0), _point("b", 2000))

    results = await asyncio.gather(
        subscription.close(),
        subscription.close(),
        return_exceptions=True,
    )

    assert results == [None, None]
    assert session.delete_attempts == [1, 2]


@pytest.mark.asyncio
async def test_close_after_standalone_deregister_does_not_repeat(fake_pyads: None) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    subscription = await _start(session, _point("a", 0))

    await asyncio.wait_for(subscription.deregister(), timeout=1.0)
    assert session.deleted == [1]
    await asyncio.wait_for(subscription.close(), timeout=1.0)
    assert session.delete_attempts == [1]


@pytest.mark.asyncio
async def test_close_after_standalone_stop_worker_still_deregisters(
    fake_pyads: None,
) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    subscription = await _start(session, _point("a", 0))

    await asyncio.wait_for(subscription.stop_worker(), timeout=1.0)
    assert subscription._worker_task is None
    assert session.deleted == []
    await asyncio.wait_for(subscription.close(), timeout=1.0)
    assert session.deleted == [1]


@pytest.mark.asyncio
async def test_failed_deregistration_keeps_cleaning_and_records_unconfirmed(
    fake_pyads: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    session.fail_del = {1}
    subscription = await _start(session, _point("a", 0), _point("b", 2000))

    await asyncio.wait_for(subscription.close(), timeout=1.0)

    # 独立资源：Handle 1 注销失败不阻断 Handle 2。
    assert session.deleted == [2]
    assert session.delete_attempts == [1, 2]
    assert [handle[0] for handle in subscription.unconfirmed_handles] == [1]
    assert "unconfirmed notification handle" in caplog.text


@pytest.mark.asyncio
async def test_close_caller_cancelled_cleanup_continues(fake_pyads: None) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    session.hold_del = {1}  # 第一个注销命令阻塞，制造中途取消窗口
    subscription = await _start(session, _point("a", 0), _point("b", 2000))

    close_task = asyncio.create_task(subscription.close())
    await _wait_event(session.del_started[1])
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    # 调用方取消时 Handle 1 的注销仍在后台线程；放行后清理必须继续，
    # 再次 close 收敛到同一轮清理而不是重复注销。
    session.release_all_del()
    await asyncio.wait_for(subscription.close(), timeout=1.0)

    assert session.delete_attempts == [1, 2]
    assert session.deleted == [1, 2]
    assert subscription._worker_task is None


@pytest.mark.asyncio
async def test_late_native_notification_after_close_is_not_delivered(
    fake_pyads: None,
) -> None:
    del fake_pyads
    session = _BlockingSession()
    session.add_release.set()
    received: list[float] = []

    async def on_sample(samples: tuple[ProtocolSample, ...]) -> None:
        for sample in samples:
            received.append(float(sample.value))  # type: ignore[arg-type]

    subscription = ADSSubscription(
        _AdsSession(session),
        (_point("a", 0),),
        on_sample,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    await subscription.start()
    late_callback = session.callbacks[1]
    await subscription.close()

    late_callback(1, (0x4020, 0), None, struct.pack("<f", 9.0))
    await asyncio.sleep(0.05)

    assert received == []
    assert subscription._queue.empty()


@pytest.mark.asyncio
async def test_cancelled_start_with_late_successful_registration_deletes_handle(
    fake_pyads: None,
) -> None:
    """注册协程被取消但同步线程晚到成功：Handle 必须被接住并注销。"""
    del fake_pyads
    session = _BlockingSession()  # add_release 初始不放行
    subscription = ADSSubscription(
        _AdsSession(session),
        (_point("a", 0),),
        _noop,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    start_task = asyncio.create_task(subscription.start())
    await _wait_event(session.add_started)

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    # 此刻“服务端已注册、客户端刚拿到 Handle”的窗口发生；放行后 close
    # 的注销阶段必须等待该结果并清理。
    session.add_release.set()
    await asyncio.wait_for(subscription.close(), timeout=2.0)

    assert session.deleted == [1]
    assert session.callbacks == {}
    assert subscription.unconfirmed_handles == ()


@pytest.mark.asyncio
async def test_cancelled_start_with_late_failed_registration_records_nothing(
    fake_pyads: None,
) -> None:
    """注册结果明确失败时不得虚构 Handle，也不得有注销命令。"""
    del fake_pyads
    session = _BlockingSession()
    session.add_raises = ConnectionError("device rejected")
    subscription = ADSSubscription(
        _AdsSession(session),
        (_point("a", 0),),
        _noop,
        cycle_time=0.1,
        loop=asyncio.get_running_loop(),
    )
    start_task = asyncio.create_task(subscription.start())
    await _wait_event(session.add_started)

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    session.add_release.set()
    await asyncio.wait_for(subscription.close(), timeout=2.0)

    assert session.callbacks == {}
    assert session.delete_attempts == []
    assert subscription.unconfirmed_handles == ()
