"""路由级资源生命周期并发安全的确定性测试（本轮收尾 T1–T6）。

pyads 3.6.0 在 Linux 上 ``Connection.open()`` = 私有端口 + ``adsAddRoute``，
``Connection.close()`` = 先 ``adsDelRoute`` 再关私有端口；AdsLib 路由按目标
Net ID 进程级共享、无引用计数。本文件不依赖真实时间预算，而用
``threading.Event`` 精确卡住 open/close 的物理段，验证：

- T1 物理关闭在「启动瞬间」即占据路由（而非异步超时后才登记），同路由
  建链必须等到 adsDelRoute 物理收敛；
- T2 路由检查与 open 之间不存在 TOCTOU：占用检查在路由锁+进程闸门内，
  同 Net ID 第二活动 Driver 被 ConfigError 拒绝；
- T3 三个 Driver 共用 Net ID：只允许单活动会话，关闭后可顺序复用；
- T4 close_local() 对活动/建链中/物理关闭中的会话原子拒绝，全部收敛后
  才拆除，拆除后建链快速失败；
- T5 不同 Net ID 的建链/读写/健康不被另一设备的慢速关闭阻塞；
- T6 等待取消、等待超时、迟到成败、多等待者、重复 close 的状态一致性，
  无孤儿任务与未观察异常。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest

from core.application import ConfigError
from core.application.errors import ProtocolError
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.protocol.ads import driver as driver_module
from core.infrastructure.protocol.ads.driver import ADSDriver, _route_state
from tests.support.units import NONE_UNIT

pytestmark = [pytest.mark.ads]

_GROUP = 0x4020
_NET = "192.0.2.10.1.1"
_NET_OTHER = "192.0.2.20.1.1"


class ControlledConnection:
    """open/close 物理段可被线程级 Event 精确控制的 pyads 连接 fake。"""

    def __init__(self, name: str, order: list[str]) -> None:
        self.name = name
        self._order = order
        self.is_open = False
        self.open_calls = 0
        self.close_calls = 0
        self.open_started = threading.Event()
        self.open_gate = threading.Event()
        self.close_started = threading.Event()
        self.close_gate = threading.Event()
        self._lock = threading.Lock()

    def _log(self, event: str) -> None:
        with self._lock:
            self._order.append(f"{self.name}:{event}")

    def open(self) -> None:
        with self._lock:
            self.open_calls += 1
        self._log("open-start")
        self.open_started.set()
        if not self.open_gate.wait(timeout=5.0):
            raise AssertionError(f"{self.name}.open() was never released")
        self.is_open = True
        self._log("open-end")

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
        self._log("close-start")
        self.close_started.set()
        if not self.close_gate.wait(timeout=5.0):
            raise AssertionError(f"{self.name}.close() was never released")
        self.is_open = False
        self._log("close-end")


def _point(name: str, offset: int) -> Point:
    return Point(
        point_id=name,
        business_point_id=BusinessPointId(name),
        source_unit=NONE_UNIT,
        access=PointAccess.READ_WRITE,
        ext={"data_type": "DINT", "index_group": _GROUP, "index_offset": offset},
    )


def _driver(*, net_id: str = _NET, host_last: int = 10) -> ADSDriver:
    points = {"a": _point("a", 0), "b": _point("b", 8192)}
    return ADSDriver(
        ConnectionEndpoint(f"192.0.2.{host_last}", 851),
        PointTable(PointTableId("route-life"), Protocol("ads"), points),
        {"target_net_id": net_id, "max_block_bytes": 1024, "timeout": 2.0},
    )


def _factory(order: list[str], *, open_auto: bool = True) -> Callable[..., ControlledConnection]:
    counter = {"n": 0}

    def factory(*_args: object, **_kwargs: object) -> ControlledConnection:
        counter["n"] += 1
        conn = ControlledConnection(f"c{counter['n']}", order)
        if open_auto:
            conn.open_gate.set()
        conn.close_gate.set()
        return conn

    return factory


async def _wait_event(event: threading.Event, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if event.wait(0.01):
            return
        await asyncio.sleep(0)
    raise AssertionError("threading event was not set within timeout")


async def _connect_ready(driver: ADSDriver, order: list[str]) -> ControlledConnection:
    """让 driver 正常建链并返回其底层可控连接。"""
    created: list[ControlledConnection] = []

    def factory(*_args: object, **_kwargs: object) -> ControlledConnection:
        conn = ControlledConnection("A", order)
        conn.open_gate.set()
        conn.close_gate.set()
        created.append(conn)
        return conn

    driver._new_connection = factory  # type: ignore[method-assign]
    await asyncio.wait_for(driver.connect(), timeout=3.0)
    assert driver.is_open()
    return created[0]


# ---- T1 -----------------------------------------------------------------


async def test_t1_physical_close_blocks_same_route_before_any_timeout() -> None:
    """物理 close 一启动（而非异步预算耗尽后）即占据同路由，新连接不得 open。"""
    order: list[str] = []
    driver_a = _driver()
    conn_a = await _connect_ready(driver_a, order)
    conn_a.close_gate = threading.Event()  # 关闭物理段挂起

    driver_b = _driver()
    driver_b._new_connection = _factory(order)  # type: ignore[method-assign]
    route = _route_state(_NET)

    close_task = asyncio.create_task(driver_a.close())
    await _wait_event(conn_a.close_started)

    # 物理 close 刚启动、远未到任何异步超时：路由锁已被 close 持有，
    # closing 登记已存在，active 仍保留（覆盖摘除→adsDelRoute 窗口）。
    assert route.op_lock.locked()
    assert route.closing, "physical close must be registered before close() starts"
    assert driver_a in route.active

    connect_b = asyncio.create_task(driver_b.connect())
    done, pending = await asyncio.wait({connect_b}, timeout=0.5)
    assert not done, "B must not connect while A physical close is in flight"
    assert not any(":open-start" in event for event in order if event.startswith("c"))

    conn_a.close_gate.set()
    await asyncio.wait_for(close_task, timeout=3.0)
    await asyncio.wait_for(connect_b, timeout=3.0)
    assert order.index("A:close-end") < order.index("c1:open-start")
    assert not route.op_lock.locked()
    await asyncio.wait_for(driver_b.close(), timeout=3.0)


# ---- T2 -----------------------------------------------------------------


async def test_t2_duplicate_active_driver_rejected_without_opening() -> None:
    """同 Net ID 第二活动 Driver 在准入点被 ConfigError 拒绝，不创建连接。"""
    order: list[str] = []
    driver_a = _driver()
    await _connect_ready(driver_a, order)

    driver_b = _driver(host_last=11)
    created = threading.Event()

    def factory(*_args: object, **_kwargs: object) -> ControlledConnection:
        created.set()
        raise AssertionError("a duplicate driver must never reach connection.open()")

    driver_b._new_connection = factory  # type: ignore[method-assign]

    with pytest.raises(ConfigError, match="already in use by another active"):
        await asyncio.wait_for(driver_b.connect(), timeout=2.0)
    assert not created.is_set()
    # A 的采集完全正常。
    sample = await asyncio.wait_for(driver_a.read_one("a"), timeout=2.0)
    assert sample.value == 11
    await asyncio.wait_for(driver_a.close(), timeout=3.0)


async def test_t2_close_then_connect_ordering_is_atomic_under_concurrency() -> None:
    """connect 与 close 并发：谁先取得路由锁都不会出现 open/delRoute 交错。"""
    order: list[str] = []
    driver_a = _driver()
    conn_a = await _connect_ready(driver_a, order)
    conn_a.close_gate = threading.Event()
    driver_b = _driver(host_last=12)
    driver_b._new_connection = _factory(order)  # type: ignore[method-assign]

    close_task = asyncio.create_task(driver_a.close())
    await _wait_event(conn_a.close_started)
    connect_b = asyncio.create_task(driver_b.connect())
    await asyncio.sleep(0.2)
    assert not connect_b.done()
    conn_a.close_gate.set()
    await asyncio.wait_for(asyncio.gather(close_task, connect_b), timeout=3.0)
    # 旧连接物理关闭严格先于新连接 open；锁已释放且登记清空。
    assert order.index("A:close-end") < order.index("c1:open-start")
    route = _route_state(_NET)
    assert not route.op_lock.locked() and not route.closing
    await asyncio.wait_for(driver_b.close(), timeout=3.0)


# ---- T3 -----------------------------------------------------------------


async def test_t3_three_drivers_same_net_id_single_active_and_sequential_reuse() -> None:
    """三个 Driver 共用 Net ID：同时刻仅一个活动；关闭后顺序复用正常。"""
    order: list[str] = []
    driver_a = _driver(host_last=10)
    driver_b = _driver(host_last=11)
    driver_c = _driver(host_last=12)
    for index, driver in enumerate((driver_a, driver_b, driver_c)):
        driver._new_connection = _factory(order)  # type: ignore[method-assign]
        # 工厂闭包共享计数器，名字仍唯一；这里仅要求建链行为正确。
        del index

    await asyncio.wait_for(driver_a.connect(), timeout=3.0)
    with pytest.raises(ConfigError):
        await asyncio.wait_for(driver_b.connect(), timeout=2.0)
    with pytest.raises(ConfigError):
        await asyncio.wait_for(driver_c.connect(), timeout=2.0)

    await asyncio.wait_for(driver_a.close(), timeout=3.0)
    # A 释放后 B 可顺序复用同一路由。
    await asyncio.wait_for(driver_b.connect(), timeout=3.0)
    health = await asyncio.wait_for(driver_b.health(), timeout=2.0)
    assert health.healthy
    sample = await asyncio.wait_for(driver_b.read_one("a"), timeout=2.0)
    assert sample.value == 11
    await asyncio.wait_for(driver_b.close(), timeout=3.0)
    await asyncio.wait_for(driver_c.connect(), timeout=3.0)
    assert driver_c.is_open()
    await asyncio.wait_for(driver_c.close(), timeout=3.0)

    # 同一 Driver 自身重连不受独占策略影响。
    await asyncio.wait_for(driver_a.connect(), timeout=3.0)
    assert driver_a.is_open()
    await asyncio.wait_for(driver_a.close(), timeout=3.0)
    route = _route_state(_NET)
    assert not route.active and not route.closing and not route.op_lock.locked()


# ---- T4 -----------------------------------------------------------------


async def test_t4_close_local_rejects_active_session() -> None:
    """存在活动 Driver 时 close_local 原子拒绝。"""
    order: list[str] = []
    driver_a = _driver()
    await _connect_ready(driver_a, order)
    with pytest.raises(ProtocolError, match="active or physical close"):
        await ADSDriver.close_local()
    await asyncio.wait_for(driver_a.close(), timeout=3.0)


async def test_t4_close_local_waits_then_rejects_session_mid_connect() -> None:
    """建链停在 open 物理段时：close_local 在闸门外等，建链完成后拒绝拆除。"""
    order: list[str] = []
    driver_a = _driver()
    conn_box: list[ControlledConnection] = []

    def factory(*_args: object, **_kwargs: object) -> ControlledConnection:
        conn = ControlledConnection("A", order)
        conn.close_gate.set()  # open 挂起期间不放行 close
        conn_box.append(conn)
        return conn

    driver_a._new_connection = factory  # type: ignore[method-assign]
    connect_task = asyncio.create_task(driver_a.connect())
    await asyncio.sleep(0.2)
    assert conn_box, "connect must have reached connection factory"
    await _wait_event(conn_box[0].open_started)

    close_local = asyncio.create_task(ADSDriver.close_local())
    _, pending = await asyncio.wait({close_local}, timeout=0.4)
    assert close_local in pending, "close_local must not tear down while open is in flight"

    conn_box[0].open_gate.set()
    await asyncio.wait_for(connect_task, timeout=3.0)
    with pytest.raises(ProtocolError):
        await asyncio.wait_for(close_local, timeout=2.0)
    await asyncio.wait_for(driver_a.close(), timeout=3.0)


async def test_t4_close_local_rejects_physical_close_in_flight() -> None:
    """物理关闭已启动但未收敛时 close_local 拒绝，并点名该路由。"""
    order: list[str] = []
    driver_a = _driver()
    conn_a = await _connect_ready(driver_a, order)
    conn_a.close_gate = threading.Event()
    close_task = asyncio.create_task(driver_a.close())
    await _wait_event(conn_a.close_started)

    with pytest.raises(ProtocolError, match=_NET.replace(".", r"\.")):
        await ADSDriver.close_local()
    # 拒绝不影响物理关闭继续收敛。
    conn_a.close_gate.set()
    await asyncio.wait_for(close_task, timeout=3.0)
    # 全部收敛后拆除成功。
    await ADSDriver.close_local()


async def test_t4_connect_fails_after_close_local_until_reinitialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拆除闩锁：新连接快速失败；initialize_local 复位后恢复。"""
    order: list[str] = []
    driver_a = _driver()
    driver_a._new_connection = _factory(order)  # type: ignore[method-assign]
    # 无任何活动/关闭会话：拆除成功并闩锁。
    await ADSDriver.close_local()
    with pytest.raises(ProtocolError, match="has been closed"):
        await asyncio.wait_for(driver_a.connect(), timeout=2.0)
    assert order == []

    # 重新初始化（用 fake pyads，不触碰真实 AdsLib）即复位闩锁。
    fake = type(
        "FakePyads",
        (),
        {
            "open_port": staticmethod(lambda: None),
            "close_port": staticmethod(lambda: None),
            "set_local_address": staticmethod(lambda _value: None),
        },
    )()
    monkeypatch.setattr(driver_module, "pyads", fake)
    from core.application.settings import ADSLocalConfig

    await ADSDriver.initialize_local(ADSLocalConfig("192.0.2.1.1.1", "192.0.2.1"))
    await asyncio.wait_for(driver_a.connect(), timeout=2.0)
    await asyncio.wait_for(driver_a.close(), timeout=3.0)
    await ADSDriver.close_local()


# ---- T5 -----------------------------------------------------------------


async def test_t5_slow_close_on_one_route_does_not_block_other_route() -> None:
    """不同 Net ID：A 的慢速物理关闭不阻塞 B 的建链/读/健康。"""
    order: list[str] = []
    driver_a = _driver(net_id=_NET, host_last=10)
    conn_a = await _connect_ready(driver_a, order)
    conn_a.close_gate = threading.Event()

    driver_b = _driver(net_id=_NET_OTHER, host_last=20)
    driver_b._new_connection = _factory(
        [],
    )  # type: ignore[method-assign]

    close_a = asyncio.create_task(driver_a.close())
    await _wait_event(conn_a.close_started)
    started = time.monotonic()
    await asyncio.wait_for(driver_b.connect(), timeout=2.0)
    health = await asyncio.wait_for(driver_b.health(), timeout=2.0)
    sample = await asyncio.wait_for(driver_b.read_one("b"), timeout=2.0)
    elapsed = time.monotonic() - started
    assert health.healthy and sample.value == 8192 + 11
    # B 的完整建链+读没有等待 A 的关闭预算（正常在亚秒级；给宽上界）。
    assert elapsed < 1.5
    await asyncio.wait_for(driver_b.close(), timeout=3.0)
    conn_a.close_gate.set()
    await asyncio.wait_for(close_a, timeout=3.0)


# ---- T6 -----------------------------------------------------------------


async def test_t6_cancelled_waiter_keeps_route_reservation_and_late_settle() -> None:
    """等待者取消不释放 close 的路由所有权；迟到收敛后下一等待者成功。"""
    order: list[str] = []
    driver_a = _driver()
    conn_a = await _connect_ready(driver_a, order)
    conn_a.close_gate = threading.Event()
    driver_b = _driver(host_last=11)
    driver_b._new_connection = _factory(order)  # type: ignore[method-assign]
    driver_c = _driver(host_last=12)
    driver_c._new_connection = _factory(order)  # type: ignore[method-assign]
    route = _route_state(_NET)

    close_a = asyncio.create_task(driver_a.close())
    await _wait_event(conn_a.close_started)
    wait_b = asyncio.create_task(driver_b.connect())
    await asyncio.sleep(0.2)
    assert not wait_b.done()
    wait_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_b
    # 取消等待者不改变 close 方的路由所有权与登记。
    assert route.op_lock.locked() and route.closing and driver_a in route.active

    wait_c = asyncio.create_task(driver_c.connect())
    await asyncio.sleep(0.2)
    assert not wait_c.done()
    conn_a.close_gate.set()
    await asyncio.wait_for(close_a, timeout=3.0)
    await asyncio.wait_for(wait_c, timeout=3.0)
    assert driver_c.is_open()
    await asyncio.wait_for(driver_c.close(), timeout=3.0)
    # 无孤儿任务。
    current = asyncio.current_task()
    await asyncio.sleep(0.1)
    leaked = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert not leaked


async def test_t6_connect_wait_is_bounded_with_explicit_error() -> None:
    """路由锁长期不释放时建链等待有界，并明确报告恢复受阻（不强行建链）。"""
    driver_a = _driver()
    route = _route_state(_NET)
    # 模拟上一轮物理 close 未收敛：路由锁被占住。
    await route.op_lock.acquire()
    try:
        with pytest.raises(ProtocolError, match="has not converged"):
            await asyncio.wait_for(driver_a.connect(), timeout=9.0)
    finally:
        route.op_lock.release()
    assert not route.op_lock.locked()


async def test_t6_multiple_waiters_one_wins_others_rejected_all_settle() -> None:
    """多个等待者：关闭收敛后恰好一个建链成功，其余被独占策略拒绝。"""
    order: list[str] = []
    driver_a = _driver()
    conn_a = await _connect_ready(driver_a, order)
    conn_a.close_gate = threading.Event()

    close_a = asyncio.create_task(driver_a.close())
    await _wait_event(conn_a.close_started)
    # 等待者必须在 close 已持有路由锁之后排队：此时 A 尚在物理关闭，
    # 它们应被挡到关闭收敛为止。
    waiters = []
    for host_last in (21, 22, 23):
        driver = _driver(host_last=host_last)
        driver._new_connection = _factory(order)  # type: ignore[method-assign]
        waiters.append((driver, asyncio.create_task(driver.connect())))
    await asyncio.sleep(0.3)
    assert all(not task.done() for _, task in waiters)
    conn_a.close_gate.set()
    await asyncio.wait_for(close_a, timeout=3.0)

    results = await asyncio.gather(*(task for _, task in waiters), return_exceptions=True)
    successes = [r for r in results if r is None]
    rejections = [r for r in results if isinstance(r, ConfigError)]
    assert len(successes) == 1 and len(rejections) == 2
    route = _route_state(_NET)
    active_driver = next(driver for driver, task in waiters if task.exception() is None)
    await asyncio.wait_for(active_driver.close(), timeout=3.0)
    assert not route.active and not route.closing and not route.op_lock.locked()


async def test_t6_repeated_close_is_idempotent_and_releases_route_once() -> None:
    """重复/并发 close：路由锁只释放一次，物理 close 只发一次。"""
    order: list[str] = []
    driver_a = _driver()
    conn_a = await _connect_ready(driver_a, order)
    conn_a.close_gate.set()
    await asyncio.gather(driver_a.close(), driver_a.close(), driver_a.close())
    await asyncio.wait_for(driver_a.close(), timeout=3.0)
    assert conn_a.close_calls == 1
    route = _route_state(_NET)
    assert not route.op_lock.locked() and not route.active and not route.closing
    # 关闭后同路由可立即重建。
    driver_a._new_connection = _factory(order)  # type: ignore[method-assign]
    await asyncio.wait_for(driver_a.connect(), timeout=3.0)
    await asyncio.wait_for(driver_a.close(), timeout=3.0)


# ---- 关闭失败隔离回归 ------------------------------------------------------


async def test_failed_physical_close_quarantines_route() -> None:
    order: list[str] = []
    driver = _driver()
    conn = await _connect_ready(driver, order)

    def broken_close() -> None:
        conn.close_calls += 1
        raise OSError("injected close failure")

    conn.close = broken_close  # type: ignore[method-assign]
    await driver.close()
    route = _route_state(_NET)
    assert route.unsafe_reason is not None
    assert not route.op_lock.locked()
    with pytest.raises(ProtocolError, match="cannot be closed"):
        await ADSDriver.close_local()

    replacement = _driver(host_last=11)
    attempted = threading.Event()

    def should_not_open(*_args: object, **_kwargs: object) -> ControlledConnection:
        attempted.set()
        raise AssertionError("unsafe route must not open")

    replacement._new_connection = should_not_open  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="quarantined"):
        await replacement.connect()
    assert not attempted.is_set()


async def test_deregister_failure_still_closes_physical_session() -> None:
    order: list[str] = []
    driver = _driver()
    conn = await _connect_ready(driver, order)

    class FailingSubscription:
        def deactivate(self) -> None:
            return None

        async def stop_worker(self) -> None:
            return None

        async def deregister(self) -> None:
            raise OSError("injected deregister failure")

    driver._subscriptions.add(FailingSubscription())  # type: ignore[arg-type]
    with pytest.raises(OSError, match="injected deregister failure"):
        await driver.close()
    assert conn.close_calls == 1
    route = _route_state(_NET)
    assert route.unsafe_reason is None
    assert not route.op_lock.locked()


async def test_inline_cleanup_exception_quarantines_route() -> None:
    """建链失败后的 inline close 抛异常，旧路由不得被再次开放。"""
    order: list[str] = []
    driver = _driver()

    class BrokenConnection(ControlledConnection):
        def read_state(self) -> tuple[int, int]:
            raise OSError("injected health failure")

        def close(self) -> None:
            raise OSError("injected inline close failure")

    def factory(*_args: object, **_kwargs: object) -> BrokenConnection:
        connection = BrokenConnection("broken", order)
        connection.open_gate.set()
        return connection

    driver._new_connection = factory  # type: ignore[method-assign]
    with pytest.raises(OSError, match="injected inline close failure"):
        await driver.connect()
    route = _route_state(_NET)
    assert route.unsafe_reason is not None

    replacement = _driver(host_last=11)
    attempted = threading.Event()

    def prohibited_factory(*_args: object, **_kwargs: object) -> ControlledConnection:
        attempted.set()
        raise AssertionError("quarantined route must not open")

    replacement._new_connection = prohibited_factory  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="quarantined"):
        await replacement.connect()
    assert not attempted.is_set()
