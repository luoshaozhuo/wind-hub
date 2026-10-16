"""关闭/重连与同步工作线程竞态的确定性测试（Component，可控阻塞 fake）。

这些用例用线程级阻塞 fake 复现“asyncio 已超时/取消、pyads 同步线程
仍在运行”的真实窗口，验证：

- 外层超时后旧线程继续持有连接，``close`` 等其结束才关闭端口（R13）；
- ``close`` 与 ``connect`` 并发时，旧会话路由/端口关闭先于新会话建立（R02）；
- close 与 health/read_many/write_many 并发均有界完成（R04/R05/R06），
  写入不自动重发；
- 旧订阅迟到原生回调不投递到新会话（R10）；
- Worker 正在处理批次时关闭：取消 Worker、清空队列（R14）；
- shutdown 等待超过 grace 放弃关闭端口而不是永久等待；
- subscribe 注册途中取消：晚到成功的 Handle 被注销，且只注销一次（R07）。
"""

from __future__ import annotations

import asyncio
import struct
import threading
from collections.abc import Callable

import pytest
from pyads.constants import ADSIGRP_SUMUP_WRITE  # type: ignore[import-untyped]

import core.infrastructure.protocol.ads.subscription as subscription_module
from core.application.errors import ProtocolError
from core.application.protocol_contract import ProtocolSample, ProtocolWrite
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

_GROUP = 0x4020


def _point(name: str, offset: int) -> Point:
    return Point(
        point_id=name,
        business_point_id=BusinessPointId(name),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "DINT", "index_group": _GROUP, "index_offset": offset},
    )


def _driver(*, max_block_bytes: int = 1024) -> ADSDriver:
    points = {"a": _point("a", 0), "b": _point("b", 8192)}
    return ADSDriver(
        ConnectionEndpoint("192.0.2.20", 801),
        PointTable(PointTableId("races"), Protocol("ads"), points),
        {"max_block_bytes": max_block_bytes},
    )


async def _wait_event(event: threading.Event, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if event.wait(0.01):
            return
        await asyncio.sleep(0)
    raise AssertionError("threading event was not set within timeout")


class _ThreadedConnection:
    """支持线程级阻塞、顺序记录与 Sum Read/Write 的 pyads 连接 fake。"""

    def __init__(self, name: str, order: list[str], *, healthy: bool = True) -> None:
        self.name = name
        self._order = order
        self.is_open = False
        self.close_calls = 0
        self.healthy = healthy
        self.read_started = threading.Event()
        self.read_gate = threading.Event()
        self.read_values = (11, 22)
        self.write_started = threading.Event()
        self.write_gate = threading.Event()
        self.state_started = threading.Event()
        self.state_gate = threading.Event()
        self.add_started = threading.Event()
        self.add_gate = threading.Event()
        self.write_calls: list[tuple[int, int, object]] = []
        self.sum_write_calls: list[int] = []
        self.callbacks: dict[int, Callable[..., None]] = {}
        self.deleted: list[int] = []
        self._next = 1
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
        self.state_started.set()
        self.state_gate.wait(timeout=5.0)
        self._log("read_state")
        return (5, 0) if self.healthy else (6, 0)

    def read(self, group: int, offset: int, datatype: object) -> int:
        del group, datatype
        self._log("read-start")
        self.read_started.set()
        if not self.read_gate.wait(timeout=5.0):
            raise TimeoutError("fake read stuck")
        self._log("read-end")
        return offset + 11

    def write(self, group: int, offset: int, value: object, datatype: object) -> None:
        del datatype
        with self._lock:
            self.write_calls.append((group, offset, value))
        self.write_started.set()
        if not self.write_gate.wait(timeout=5.0):
            raise TimeoutError("fake write stuck")

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
        if index_group == ADSIGRP_SUMUP_WRITE:
            self.sum_write_calls.append(count)
            self.write_started.set()
            self.write_gate.wait(timeout=5.0)
            return b"\x00" * (4 * count)
        # Sum Read 路径同样可被门控阻塞，以复现旧线程迟到窗口。
        self._log("read_write-start")
        self.read_started.set()
        self.read_gate.wait(timeout=5.0)
        self._log("read_write-end")
        codes = b"\x00" * (4 * count)
        data = struct.pack("<" + "i" * count, *self.read_values[:count])
        return codes + data

    def notification(
        self, datatype: object
    ) -> Callable[[Callable[..., None]], Callable[..., None]]:
        del datatype
        return lambda callback: callback

    def add_device_notification(
        self, address: object, attr: object, callback: Callable[..., None]
    ) -> tuple[int, None]:
        del address, attr
        self.add_started.set()
        self.add_gate.wait(timeout=5.0)
        with self._lock:
            handle = self._next
            self._next += 1
            self.callbacks[handle] = callback
        self._log(f"add-{handle}")
        return (handle, None)

    def del_device_notification(self, handle: int, user_handle: object) -> None:
        del user_handle
        with self._lock:
            self.callbacks.pop(handle, None)
            self.deleted.append(handle)
        self._log(f"del-{handle}")

    def close(self) -> None:
        self._log("close")
        self.close_calls += 1
        self.is_open = False

    def fire(self, handle: int, value: int) -> None:
        raw = struct.pack("<i", value)
        self.callbacks[handle](handle, (_GROUP, 0), None, raw)


class _Attrib:
    def __init__(self, length: int, *, trans_mode: int, cycle_time: float) -> None:
        self.length = length
        self.trans_mode = trans_mode
        self.cycle_time = cycle_time


class _FakePyads:
    ADSTRANS_SERVERCYCLE = 3
    NotificationAttrib = _Attrib


@pytest.fixture
def fake_pyads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subscription_module, "pyads", _FakePyads)


def _attach(driver: ADSDriver, connection: _ThreadedConnection) -> _AdsSession:
    connection.open()  # 对齐真实 connect：health 等路径要求 session.is_open
    session = _AdsSession(connection)
    driver._session = session
    driver._connected = True
    return session


async def _noop(_sample: ProtocolSample) -> None:
    pass


def _new_connection_factory(
    order: list[str],
) -> Callable[..., _ThreadedConnection]:
    counter = {"n": 0}

    def factory(*_args: object, **_kwargs: object) -> _ThreadedConnection:
        counter["n"] += 1
        connection = _ThreadedConnection(f"new{counter['n']}", order)
        connection.read_gate.set()
        connection.state_gate.set()
        connection.write_gate.set()
        connection.add_gate.set()
        return connection

    return factory


@pytest.mark.asyncio
async def test_outer_timeout_waits_inflight_thread_before_closing_port(
    fake_pyads: None,
) -> None:
    """外层 wait_for 超时不等于底层线程结束：close 必须等待并只关一次端口。"""
    del fake_pyads
    order: list[str] = []
    driver = _driver()
    connection = _ThreadedConnection("old", order)
    connection.state_gate.set()
    connection.write_gate.set()
    _attach(driver, connection)

    read_task = asyncio.create_task(driver.read_one("a"))
    await _wait_event(connection.read_started)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(read_task, timeout=0.2)
    # 协程层已取消超时读，但同步线程仍持有会话命令锁。

    # 旧读线程仍持有会话命令锁：close 在其返回前不得关闭端口。
    close_task = asyncio.create_task(driver.close())
    await asyncio.sleep(0.1)
    assert connection.close_calls == 0
    connection.read_gate.set()
    await asyncio.wait_for(close_task, timeout=2.0)
    assert connection.close_calls == 1
    assert order.index("old:read-end") < order.index("old:close")
    # 被取消的读结果不会作为成功样本返回。
    with pytest.raises(asyncio.CancelledError):
        await read_task


@pytest.mark.asyncio
async def test_r02_connect_waits_for_old_session_close_and_isolates_state(
    fake_pyads: None,
) -> None:
    """close 与 connect 并发：新会话 open 必须在旧会话 close 之后。"""
    del fake_pyads
    order: list[str] = []
    driver = _driver()
    old = _ThreadedConnection("old", order)
    old.state_gate.set()
    old.write_gate.set()
    _attach(driver, old)
    driver._new_connection = _new_connection_factory(order)  # type: ignore[method-assign]

    read_task = asyncio.create_task(driver.read_one("a"))
    await _wait_event(old.read_started)

    close_task = asyncio.create_task(driver.close())
    connect_task = asyncio.create_task(driver.connect())
    await asyncio.sleep(0.1)
    # 旧读未结束，两个收尾动作都不得越过它。
    assert not any("new1:open" in event for event in order)
    # 超时读在调用方被取消，但同步线程仍在跑，稍后放行。
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(read_task), timeout=0.1)
    old.read_gate.set()
    results = await asyncio.wait_for(
        asyncio.gather(close_task, connect_task, return_exceptions=True),
        timeout=3.0,
    )
    assert results == [None, None], results

    assert order.index("old:close") < order.index("new1:open")
    assert old.close_calls == 1
    new_session = driver._session
    assert new_session is not None and new_session.connection is not old
    # 新会话读到的是新连接的数据，旧线程迟到返回不污染新状态。
    sample = await driver.read_one("a")
    assert sample.value == 11 and sample.quality.name == "GOOD"
    await driver.close()


@pytest.mark.asyncio
async def test_r04_close_concurrent_with_health_is_bounded(fake_pyads: None) -> None:
    del fake_pyads
    order: list[str] = []
    driver = _driver()
    connection = _ThreadedConnection("old", order)
    connection.read_gate.set()
    connection.write_gate.set()
    _attach(driver, connection)

    health_task = asyncio.create_task(driver.health())
    await _wait_event(connection.state_started)
    close_task = asyncio.create_task(driver.close())
    await asyncio.sleep(0.05)
    assert connection.close_calls == 0
    connection.state_gate.set()

    health, _ = await asyncio.wait_for(asyncio.gather(health_task, close_task), timeout=3.0)
    assert connection.close_calls == 1
    assert order.index("old:read_state") < order.index("old:close")
    # 无串扰：state 返回明确，close 不抛错。
    assert health.message is not None


@pytest.mark.asyncio
async def test_r05_close_concurrent_with_sum_read_bounded(fake_pyads: None) -> None:
    del fake_pyads
    order: list[str] = []
    driver = _driver()
    connection = _ThreadedConnection("old", order)
    connection.state_gate.set()
    connection.write_gate.set()
    _attach(driver, connection)

    read_task = asyncio.create_task(driver.read_many(("a", "b")))
    await _wait_event(connection.read_started)
    close_task = asyncio.create_task(driver.close())
    await asyncio.sleep(0.05)
    connection.read_gate.set()

    result = await asyncio.wait_for(
        asyncio.gather(read_task, close_task, return_exceptions=True), timeout=3.0
    )
    sample_or_exc, close_result = result
    assert close_result is None
    assert not isinstance(sample_or_exc, BaseException) or isinstance(
        sample_or_exc, ProtocolError
    ), sample_or_exc
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_r06_close_concurrent_with_sum_write_no_resend(fake_pyads: None) -> None:
    del fake_pyads
    order: list[str] = []
    driver = _driver()
    connection = _ThreadedConnection("old", order)
    connection.state_gate.set()
    connection.read_gate.set()
    _attach(driver, connection)

    write_task = asyncio.create_task(
        driver.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    )
    await _wait_event(connection.write_started)
    close_task = asyncio.create_task(driver.close())
    connection.write_gate.set()

    result = await asyncio.wait_for(
        asyncio.gather(write_task, close_task, return_exceptions=True), timeout=3.0
    )
    write_result, close_result = result
    assert close_result is None
    assert not isinstance(write_result, BaseException), write_result
    # 一次 Sum Write（两个点），关闭流程不得自动重发任何写入。
    assert connection.sum_write_calls == [2]
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_r10_late_callback_of_old_session_does_not_reach_new_session(
    fake_pyads: None,
) -> None:
    del fake_pyads
    driver = _driver(max_block_bytes=64)
    old = _ThreadedConnection("old", [])
    old.state_gate.set()
    old.read_gate.set()
    old.write_gate.set()
    old.add_gate.set()
    _attach(driver, old)

    old_seen: list[int] = []

    async def old_cb(sample: ProtocolSample) -> None:
        old_seen.append(int(sample.value))  # type: ignore[arg-type]

    await driver.subscribe(("a",), old_cb, interval=0.05)
    old_callback = old.callbacks[1]
    await driver.close()
    assert old.deleted == [1]

    new = _ThreadedConnection("new", [])
    new.state_gate.set()
    new.read_gate.set()
    new.write_gate.set()
    new.add_gate.set()
    _attach(driver, new)
    new_seen: list[int] = []
    new_done = asyncio.Event()

    async def new_cb(sample: ProtocolSample) -> None:
        new_seen.append(int(sample.value))  # type: ignore[arg-type]
        new_done.set()

    await driver.subscribe(("a",), new_cb, interval=0.05)
    try:
        # 旧会话残留原生回调：不得进入任何新会话的投递路径。
        old_callback(1, (_GROUP, 0), None, struct.pack("<i", 99))
        await asyncio.sleep(0.05)
        assert new_seen == []
        new.fire(1, 7)
        await asyncio.wait_for(new_done.wait(), timeout=1.0)
        assert new_seen == [7]
        assert old_seen == []
    finally:
        await driver.close()


@pytest.mark.asyncio
async def test_r14_worker_processing_batch_on_close_cancels_and_drains_queue(
    fake_pyads: None,
) -> None:
    del fake_pyads
    driver = _driver()
    connection = _ThreadedConnection("old", [])
    connection.state_gate.set()
    connection.read_gate.set()
    connection.write_gate.set()
    connection.add_gate.set()
    _attach(driver, connection)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_cb(_sample: ProtocolSample) -> None:
        entered.set()
        await release.wait()

    tracked = await driver.subscribe(("a",), slow_cb, interval=0.05)
    subscription = tracked._subscription
    connection.fire(1, 1)
    await entered.wait()
    for value in range(2, 8):
        connection.fire(1, value)
    await asyncio.sleep(0.02)
    assert not subscription._queue.empty()

    await asyncio.wait_for(driver.close(), timeout=2.0)
    # Worker 在回调中被取消；关闭后队列被清空，无 Worker 任务残留。
    assert subscription._queue.empty()
    assert subscription._worker_task is None
    assert [
        task for task in asyncio.all_tasks() if "ads-subscription-worker" in task.get_name()
    ] == []
    release.set()


@pytest.mark.asyncio
async def test_session_shutdown_abandons_port_after_grace_without_infinite_wait() -> None:
    order: list[str] = []
    connection = _ThreadedConnection("old", order)
    connection.open()
    session = _AdsSession(connection)

    def blocked_read() -> None:
        connection.read_started.set()
        connection.read_gate.wait(timeout=5.0)

    thread = threading.Thread(target=lambda: session.run("read", _GROUP, 0, None))
    thread.start()
    assert connection.read_started.wait(timeout=1.0)

    # 在途命令未结束：超过 grace 放弃关闭，端口关闭命令绝不发出。
    assert session.shutdown(grace=0.05) is False
    assert connection.close_calls == 0
    assert session.run  # 会话仍可引用

    connection.read_gate.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    # 在途结束后的关闭有界且只执行一次。
    assert session.shutdown(grace=1.0) is True
    assert connection.close_calls == 1


@pytest.mark.asyncio
async def test_r07_subscribe_cancelled_then_late_handle_is_deleted_once(
    fake_pyads: None,
) -> None:
    """注册途中取消：同步线程晚到成功的 Handle 必须被清理，且只注销一次。"""
    del fake_pyads
    driver = _driver(max_block_bytes=64)
    connection = _ThreadedConnection("old", [])
    connection.state_gate.set()
    connection.read_gate.set()
    connection.write_gate.set()
    _attach(driver, connection)

    subscribe_task = asyncio.create_task(driver.subscribe(("a", "b"), _noop, interval=0.05))
    await _wait_event(connection.add_started)
    subscribe_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await subscribe_task

    # 服务端此刻可能已注册：放行后 close 必须接住晚到 Handle 并注销。
    connection.add_gate.set()
    await asyncio.wait_for(driver.close(), timeout=3.0)

    assert connection.deleted == [1]
    assert connection.callbacks == {}
