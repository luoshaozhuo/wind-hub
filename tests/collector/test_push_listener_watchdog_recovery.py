"""ROUND3 专项 B：IEC104 push 采集「Sink 派发失效 → listener 死亡 → health
反映 → Watchdog close→connect 重连 → 仅恢复 desired_running 实例（新
Listener + 显式 GI）→ Sink 恢复后数据重新到达且成功状态复位」完整闭环。

不 mock 驱动/会话/看门狗：真实 IEC104Driver + _IEC104Session（消息级
FakeConnection 工厂，每次重连产出一条全新连接）+ DeviceRuntime /
TaskRuntime 真实装配，Watchdog 以 100ms 周期运行。唯一注入点是
AcquisitionEngine 的 SinkDispatchPort——模拟 Sink 交付异常/阻塞，与生产
中交付边界失败同构。

v2 下每设备恰好一个采集实例（task_id == device_id == "dev1"）：旧版
「同设备 t1/t2 多实例恢复隔离」场景在架构上不再存在，跨设备隔离由
test_watchdog_restore 的故障/健康设备对覆盖；手动 STOP 不复活由
test_watchdog_restore 的同设备停止意图测试覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pytest
from hat.drivers.iec104 import common as iec104

from collector.application.config import CollectionTask, RuntimeParams
from collector.application.device_runtime import DeviceRuntime
from collector.application.session import CollectorDeviceSession
from collector.application.task_instance import TaskInstanceState
from collector.application.task_runtime import TaskRuntime
from collector.domain.acquisition import AcquisitionEngine
from core.domain import DeviceGroupId
from tests.support.iec104_fake import (
    FakeConnection,
    floating_data_msg,
    interrogation_reply,
    make_driver,
    make_point,
)
from tests.support.new_collector import make_collector_config, make_session
from tests.support.wait import wait_until

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

WATCHDOG_INTERVAL = 0.1


class _AutoGIConnection(FakeConnection):
    """收到总召命令即自动回 ACT_CON + ACT_TERM（无数据），使真实 GI 走完。"""

    def __init__(self) -> None:
        super().__init__()
        self.gi_requests: list[int] = []

    async def send(self, messages: Iterable[Any]) -> None:
        await super().send(messages)
        for msg in list(messages):
            if isinstance(msg, iec104.InterrogationMsg):
                self.gi_requests.append(msg.request)
                self.deliver(
                    [
                        interrogation_reply(
                            1,
                            request=msg.request,
                            cause=iec104.CommandResCause.ACTIVATION_CONFIRMATION,
                        ),
                        interrogation_reply(
                            1,
                            request=msg.request,
                            cause=iec104.CommandResCause.ACTIVATION_TERMINATION,
                        ),
                    ]
                )


class _ControllableDispatch:
    """可切换失败的 Sink 派发端口，并记录成功交付的点值。"""

    def __init__(self) -> None:
        self.failing = False
        self.delivered: list[Any] = []
        self.calls = 0

    async def dispatch(self, task_id: str, values: list[Any]) -> None:
        del task_id
        self.calls += 1
        if self.failing:
            raise RuntimeError("sample dispatch failed")
        self.delivered.extend(values)


class _GateDispatch:
    """可阻塞的 Sink 派发端口：gate 清除时数据积压在 listener 队列。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.gate.set()
        self.delivered: list[Any] = []

    async def dispatch(self, task_id: str, values: list[Any]) -> None:
        del task_id
        await self.gate.wait()
        self.delivered.extend(values)


@dataclass
class _Assembly:
    driver: Any
    session: CollectorDeviceSession
    devices: DeviceRuntime
    tasks: TaskRuntime
    dispatch: Any
    connections: list[_AutoGIConnection]


def _device_task() -> dict[str, CollectionTask]:
    """v2：dev1 唯一采集任务（task_id == device_id）。"""
    return {
        "dev1": CollectionTask(
            task_id="dev1",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g",
            interval=1.0,
        )
    }


async def _assemble(
    monkeypatch: pytest.MonkeyPatch,
    *,
    dispatch: Any | None = None,
) -> _Assembly:
    connections: list[_AutoGIConnection] = []

    async def _factory(addr: Any, **kwargs: Any) -> _AutoGIConnection:
        del addr, kwargs
        conn = _AutoGIConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _factory)

    driver = make_driver([make_point("p1", 100)])
    params = RuntimeParams(
        connect_timeout=2.0,
        shutdown_timeout=0.5,
        watchdog_interval=WATCHDOG_INTERVAL,
        health_timeout=1.0,
    )
    config = make_collector_config(protocol="iec104", tasks=_device_task(), params=params)
    session = make_session(config, driver)

    devices = DeviceRuntime({"dev1": session}, params)
    engine = AcquisitionEngine()
    port = dispatch or _ControllableDispatch()
    engine.attach_sample_publisher(port)
    engine.attach_device_state(devices)
    tasks = TaskRuntime(dict(config.tasks), devices, engine)
    engine.attach_acquisition_state(tasks)
    devices.set_reconnect_callback(
        lambda device_id: tasks.start_device_tasks(device_id, can_restore=lambda: True),
        on_connected=tasks.prepare_device_restore,
    )

    await devices.connect_all()
    await tasks.sync_instances()
    return _Assembly(driver, session, devices, tasks, port, connections)


async def test_push_failure_watchdog_recovery_full_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asm = await _assemble(monkeypatch)
    driver, tasks = asm.driver, asm.tasks
    try:
        # 1) LISTEN 启动：显式 start 后实例 RUNNING，首条 GI 已在连接 1 上发出。
        await tasks.start_instance("dev1")
        assert tasks.instance_states()["dev1"] is TaskInstanceState.RUNNING
        conn1 = asm.connections[0]
        assert conn1.gi_requests == [20]
        assert len(driver._listeners) == 1  # noqa: SLF001
        old_listener = next(iter(driver._listeners))  # noqa: SLF001
        old_session = driver._session  # noqa: SLF001

        # 2) 成功派发记账：数据到达 Sink，执行状态成功、零连续失败。
        conn1.deliver([floating_data_msg(1, 100, 1.0)])
        await wait_until(
            lambda: True if any(v.value == 1.0 for v in asm.dispatch.delivered) else None,
            timeout=5.0,
            description="first spontaneous value reaches sink",
        )
        state = tasks.acquisition_states()["dev1"]
        assert state.last_success_at is not None
        assert state.consecutive_failures == 0
        assert state.last_error is None

        # 纯连接健康检查不创建任何额外监听/订阅，也不发网络报文。
        sent_before = len(conn1.sent_messages)
        health = await driver.health()
        assert health.healthy
        assert len(driver._listeners) == 1  # noqa: SLF001
        assert len(conn1.sent_messages) == sent_before

        # 3) 注入 Sink 派发持续异常。
        asm.dispatch.failing = True
        conn1.deliver([floating_data_msg(1, 100, 2.0)])

        # 4) listener 因回调异常死亡。
        await wait_until(
            lambda: True if old_listener.failed else None,
            timeout=5.0,
            description="listener invalidated after callback failure",
        )
        handle = tasks._acquisition_handles["dev1"]  # noqa: SLF001
        assert handle.failed

        # 5) 执行状态记账：consecutive_failures 递增、last_error 非空。
        state = tasks.acquisition_states()["dev1"]
        assert state.consecutive_failures > 0
        assert state.last_error

        # 6) IEC104 health() 显式反映 listener 死亡（故障窗口内不得假报健康）。
        health = await driver.health()
        assert not health.healthy
        assert "listener stopped" in health.message

        # 7-11) Watchdog 按阈值/退避执行 close→connect：
        #  旧连接先彻底关闭，新连接建立；旧 session/listener 不复存在；
        #  desired_running 实例在新连接上恢复；新 Listener 建立并重发显式 GI。
        await wait_until(
            lambda: True if len(asm.connections) == 2 else None,
            timeout=10.0,
            description="watchdog reconnects with a fresh connection",
        )
        conn2 = asm.connections[1]
        assert conn1.closed
        assert not conn2.closed
        assert driver.is_open()
        assert driver._session is not old_session  # noqa: SLF001
        assert old_listener not in driver._listeners  # noqa: SLF001

        await wait_until(
            lambda: (
                True if tasks.instance_states()["dev1"] is TaskInstanceState.RUNNING else None
            ),
            timeout=5.0,
            description="desired-running instance restored",
        )
        assert conn2.gi_requests == [20], "Collector must re-issue explicit GI on restore"
        assert len(driver._listeners) == 1  # noqa: SLF001
        new_listener = next(iter(driver._listeners))  # noqa: SLF001
        assert new_listener is not old_listener
        assert not new_listener.failed

        # 健康监听器不得被误判失败：恢复后无数据期间 health 为健康。
        assert (await driver.health()).healthy

        # 12-13) Sink 恢复后数据必须重新到达，且成功状态复位——
        # 不能仅凭 consecutive_failures>0 判定本项 PASS。
        asm.dispatch.failing = False
        conn2.deliver([floating_data_msg(1, 100, 3.0)])
        await wait_until(
            lambda: True if any(v.value == 3.0 for v in asm.dispatch.delivered) else None,
            timeout=5.0,
            description="spontaneous data reaches sink again after recovery",
        )
        state = tasks.acquisition_states()["dev1"]
        assert state.consecutive_failures == 0
        assert state.last_error is None

        # 无重复 listener / 句柄 / 连接泄漏。
        assert len(driver._listeners) == 1  # noqa: SLF001
        assert len(tasks._acquisition_handles) == 1  # noqa: SLF001
        assert len(asm.connections) == 2
        assert conn1.closed
    finally:
        await asm.devices.close_all()
        await driver.close()


async def test_push_listener_queue_overflow_triggers_same_recovery_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """listener 失效路径二：回调被阻塞致 256 容量队列溢出，同样必须闭环恢复。"""
    asm = await _assemble(monkeypatch, dispatch=_GateDispatch())
    driver, tasks = asm.driver, asm.tasks
    try:
        await tasks.start_instance("dev1")
        conn1 = asm.connections[0]

        # 先有一次成功交付，确认阻塞前链路健康。
        conn1.deliver([floating_data_msg(1, 100, 1.0)])
        await wait_until(
            lambda: True if any(v.value == 1.0 for v in asm.dispatch.delivered) else None,
            timeout=5.0,
            description="first value delivered before blocking sink",
        )

        # 阻塞消费端，灌满 listener 有界队列（容量 256）。
        asm.dispatch.gate.clear()
        for i in range(300):
            conn1.deliver([floating_data_msg(1, 100, 10.0 + i)])
        old_listener = next(iter(driver._listeners))  # noqa: SLF001
        await wait_until(
            lambda: True if old_listener.failed else None,
            timeout=5.0,
            description="listener invalidated after queue overflow",
        )
        assert not (await driver.health()).healthy

        # Watchdog 重建连接与监听。
        await wait_until(
            lambda: True if len(asm.connections) == 2 else None,
            timeout=10.0,
            description="watchdog reconnects after overflow",
        )
        conn2 = asm.connections[1]
        await wait_until(
            lambda: (
                True if tasks.instance_states()["dev1"] is TaskInstanceState.RUNNING else None
            ),
            timeout=5.0,
            description="instance restored after overflow reconnect",
        )

        # 解除阻塞后，新链路上的数据重新到达 Sink。
        asm.dispatch.gate.set()
        conn2.deliver([floating_data_msg(1, 100, 999.0)])
        await wait_until(
            lambda: True if any(v.value == 999.0 for v in asm.dispatch.delivered) else None,
            timeout=5.0,
            description="data reaches sink again after overflow recovery",
        )
        state = tasks.acquisition_states()["dev1"]
        assert state.consecutive_failures == 0
        assert state.last_error is None
        assert len(driver._listeners) == 1  # noqa: SLF001
        assert conn1.closed
    finally:
        asm.dispatch.gate.set()
        await asm.devices.close_all()
        await driver.close()


async def test_healthy_listener_is_never_falsely_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对照组：持续成功交付期间 listener 不得被误判，health 恒健康、无额外订阅。"""
    asm = await _assemble(monkeypatch)
    driver, tasks = asm.driver, asm.tasks
    try:
        await tasks.start_instance("dev1")
        conn = asm.connections[0]
        for i in range(10):
            conn.deliver([floating_data_msg(1, 100, float(i))])
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

        listener = next(iter(driver._listeners))  # noqa: SLF001
        assert not listener.failed
        assert not listener.task.done()
        assert (await driver.health()).healthy
        assert tasks.acquisition_states()["dev1"].consecutive_failures == 0

        # 健康检查是纯本地判断：不新增 listener，也不发任何报文。
        sent_before = len(conn.sent_messages)
        listeners_before = len(driver._listeners)
        for _ in range(3):
            assert (await driver.health()).healthy
        assert len(conn.sent_messages) == sent_before
        assert len(driver._listeners) == listeners_before
    finally:
        await asm.devices.close_all()
        await driver.close()


async def test_listener_fault_latch_survives_removal_but_not_manual_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """故障锁存契约（fa31a1e7）：

    - 正常手动 listener.close()（STOP/热重载路径）不得置故障；
    - 回调异常的故障位在 listener 离开集合后仍保持（不空集合假健康）；
    - 只有完整 close→connect 清理完成后故障位才清除。
    """
    connections: list[FakeConnection] = []

    async def _factory(addr: Any, **kwargs: Any) -> FakeConnection:
        del addr, kwargs
        conn = FakeConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _factory)
    driver = make_driver([make_point("p1", 100)])
    await driver.connect()
    try:
        received: list[Any] = []

        async def good_cb(samples: Any) -> None:
            received.extend(samples)

        handle = await driver.listen(["p1"], good_cb)
        conn = connections[0]
        conn.deliver([floating_data_msg(1, 100, 1.0)])
        await wait_until(
            lambda: True if len(received) == 1 else None,
            timeout=5.0,
            description="good listener receives one value",
        )
        assert (await driver.health()).healthy

        # 手动 STOP：关闭 listener 不得被登记为故障。
        await handle.close()
        assert driver._listeners == set()  # noqa: SLF001
        assert driver._listener_fault is False  # noqa: SLF001
        assert (await driver.health()).healthy

        # 回调死亡：故障立即锁存。
        async def bad_cb(samples: Any) -> None:
            del samples
            raise RuntimeError("boom")

        bad_handle = await driver.listen(["p1"], bad_cb)
        conn.deliver([floating_data_msg(1, 100, 2.0)])
        await wait_until(
            lambda: True if bad_handle.failed else None,
            timeout=5.0,
            description="bad listener fails",
        )
        assert driver._listener_fault is True  # noqa: SLF001
        assert not (await driver.health()).healthy

        # 故障 listener 被 prepare 路径关闭移出集合后，故障位不得丢失。
        await bad_handle.close()
        assert driver._listeners == set()  # noqa: SLF001
        assert driver._listener_fault is True  # noqa: SLF001
        assert not (await driver.health()).healthy

        # 完整 close→connect（Watchdog 恢复路径）完成后才允许清除。
        await driver.close()
        assert driver._listener_fault is False  # noqa: SLF001
        await driver.connect()
        assert driver._listeners == set()  # noqa: SLF001
        assert (await driver.health()).healthy
    finally:
        await driver.close()
