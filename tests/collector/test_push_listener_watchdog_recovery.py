"""ROUND3 专项 B：IEC104 push 采集「Sink 派发失效 → listener 死亡 → health
反映 → Watchdog close→connect 重连 → 仅恢复 desired_running 实例（新
Listener + 显式 GI）→ Sink 恢复后数据重新到达且成功状态复位」完整闭环。

不 mock 驱动/会话/看门狗：真实 IEC104Driver + _IEC104Session（消息级
FakeConnection 工厂，每次重连产出一条全新连接）+ DeviceRuntime /
TaskRuntime 真实装配，Watchdog 以 100ms 周期运行。唯一注入点是
AcquisitionEngine 的 SinkDispatchPort——模拟 Sink 交付异常/阻塞，与生产
中交付边界失败同构。
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
from core.domain import (
    BusinessPointId,
    ConnectionEndpoint,
    Device,
    DeviceGroupId,
    DeviceId,
    DeviceModelId,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from tests.support.iec104_fake import (
    FakeConnection,
    floating_data_msg,
    interrogation_reply,
    make_driver,
    make_point,
)
from tests.support.new_collector import make_collector_config, make_session
from tests.support.units import NONE_UNIT
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

    async def dispatch(self, routed: dict[str, Any]) -> None:
        self.calls += 1
        if self.failing:
            raise RuntimeError("sink dispatch failed")
        for batch in routed.values():
            self.delivered.extend(batch.values)


class _GateDispatch:
    """可阻塞的 Sink 派发端口：gate 清除时数掘积压在 listener 队列。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.gate.set()
        self.delivered: list[Any] = []

    async def dispatch(self, routed: dict[str, Any]) -> None:
        await self.gate.wait()
        for batch in routed.values():
            self.delivered.extend(batch.values)


@dataclass
class _Assembly:
    driver: Any
    session: CollectorDeviceSession
    devices: DeviceRuntime
    tasks: TaskRuntime
    dispatch: Any
    connections: list[_AutoGIConnection]


def _two_tasks() -> dict[str, CollectionTask]:
    """t1/t2 同设备同点组：t2 在测试中被显式停止，用于验证恢复范围。"""
    return {
        "t1": CollectionTask(
            task_id="t1",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g",
            interval=1.0,
            sink_ids=("s1",),
        ),
        "t2": CollectionTask(
            task_id="t2",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g",
            interval=1.0,
            sink_ids=("s1",),
        ),
    }


async def _assemble(
    monkeypatch: pytest.MonkeyPatch,
    *,
    points: list[Point] | None = None,
    task_defs: dict[str, CollectionTask] | None = None,
    dispatch: Any | None = None,
    table: PointTable | None = None,
) -> _Assembly:
    connections: list[_AutoGIConnection] = []

    async def _factory(addr: Any, **kwargs: Any) -> _AutoGIConnection:
        del addr, kwargs
        conn = _AutoGIConnection()
        connections.append(conn)
        return conn

    monkeypatch.setattr("core.infrastructure.protocol.iec104.session.iec104.connect", _factory)

    driver = make_driver(points or [make_point("p1", 100)])
    params = RuntimeParams(
        connect_timeout=2.0,
        shutdown_timeout=0.5,
        watchdog_interval=WATCHDOG_INTERVAL,
        health_timeout=1.0,
    )
    if table is None:
        config = make_collector_config(
            protocol="iec104", tasks=task_defs or _two_tasks(), params=params
        )
        session = make_session(config, driver)
        task_defs = dict(config.tasks)
    else:
        device = Device(
            device_id=DeviceId("dev1"),
            device_model_id=DeviceModelId("mod"),
            endpoint=ConnectionEndpoint("127.0.0.1", 2404),
            device_group_ids=(DeviceGroupId("dev1"),),
            subscribe_enabled=False,
            protocol_options={},
        )
        session = CollectorDeviceSession(device, table, driver, subscribe_enabled=False)
        assert task_defs is not None

    devices = DeviceRuntime({"dev1": session}, params)
    engine = AcquisitionEngine()
    port = dispatch or _ControllableDispatch()
    engine.attach_sink_dispatch(port)
    engine.attach_device_state(devices)
    tasks = TaskRuntime(task_defs, devices, engine)
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
        await tasks.start_instance("t1:dev1")
        assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
        # t2 先启动再被手动停止：重连后绝不得被自动恢复（desired_running 门控）。
        await tasks.start_instance("t2:dev1")
        assert tasks.instance_states()["t2:dev1"] is TaskInstanceState.RUNNING
        await tasks.stop_instance("t2:dev1")
        assert tasks.instance_states()["t2:dev1"] is TaskInstanceState.STOPPED
        conn1 = asm.connections[0]
        assert conn1.gi_requests == [20, 20]
        assert len(driver._listeners) == 1  # noqa: SLF001  — stop 已清退 t2 监听
        old_listener = next(iter(driver._listeners))  # noqa: SLF001
        old_session = driver._session  # noqa: SLF001

        # 2) 成功派发记账：数据到达 Sink，执行状态成功、零连续失败。
        conn1.deliver([floating_data_msg(1, 100, 1.0)])
        await wait_until(
            lambda: True if any(v.value == 1.0 for v in asm.dispatch.delivered) else None,
            timeout=5.0,
            description="first spontaneous value reaches sink",
        )
        state = tasks.acquisition_states()["t1:dev1"]
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
        handle = tasks._acquisition_handles["t1:dev1"]  # noqa: SLF001
        assert handle.failed

        # 5) 执行状态记账：consecutive_failures 递增、last_error 非空。
        state = tasks.acquisition_states()["t1:dev1"]
        assert state.consecutive_failures > 0
        assert state.last_error

        # 6) IEC104 health() 显式反映 listener 死亡（故障窗口内不得假报健康）。
        health = await driver.health()
        assert not health.healthy
        assert "listener stopped" in health.message

        # 7-11) Watchdog 按阈值/退避执行 close→connect：
        #  旧连接先彻底关闭，新连接建立；旧 session/listener 不复存在；
        #  仅 desired_running 实例被恢复（STOPPED 的 t2 不恢复）；
        #  新 Listener 建立并重新执行显式 GI。
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
                True if tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING else None
            ),
            timeout=5.0,
            description="desired-running instance restored",
        )
        assert conn2.gi_requests == [20], "Collector must re-issue explicit GI on restore"
        assert "t2:dev1" not in tasks._acquisition_handles  # noqa: SLF001
        assert tasks.instance_states()["t2:dev1"] is TaskInstanceState.STOPPED
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
        state = tasks.acquisition_states()["t1:dev1"]
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
        await tasks.start_instance("t1:dev1")
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
                True if tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING else None
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
        state = tasks.acquisition_states()["t1:dev1"]
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
        await tasks.start_instance("t1:dev1")
        conn = asm.connections[0]
        for i in range(10):
            conn.deliver([floating_data_msg(1, 100, float(i))])
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

        listener = next(iter(driver._listeners))  # noqa: SLF001
        assert not listener.failed
        assert not listener.task.done()
        assert (await driver.health()).healthy
        assert tasks.acquisition_states()["t1:dev1"].consecutive_failures == 0

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


def _two_group_table() -> PointTable:
    def point(pid: str, ioa: int, groups: tuple[str, ...]) -> Point:
        return Point(
            point_id=pid,
            business_point_id=BusinessPointId(pid),
            source_unit=NONE_UNIT,
            access=PointAccess.READ,
            ext={"ioa": ioa, "ca": 1, "type_id": "M_ME_NC_1"},
            point_groups=groups,
        )

    return PointTable(
        PointTableId("tab"),
        Protocol("iec104"),
        {
            "p1": point("p1", 100, ("g",)),
            "p2": point("p2", 101, ("g2",)),
        },
    )


async def test_restore_failure_of_one_instance_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1（round3 复现 → round4 修复验证）：同设备两个 desired_running 实例，
    重连恢复时 t1 的 listen 持续失败，不得腐蚀 t2。

    十项要求在本测试与 unit/collector/test_task_runtime.py 中逐条覆盖：
    多实例期望运行；t1 持续失败；t2 独立恢复；t1 保留 restart_pending；
    Watchdog 经恢复回调异常持续获知"恢复未完成"；后续周期只重试 t1
    （拒绝计数继续增长、t2 不重复建句柄/不重复 GI）；无额外物理重连；
    运行意图不被清除。手动 STOPPED 不恢复见主闭环测试的 t2。
    """
    task_defs = {
        "t1": CollectionTask(
            task_id="t1",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g",
            interval=1.0,
            sink_ids=("s1",),
        ),
        "t2": CollectionTask(
            task_id="t2",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g2",
            interval=1.0,
            sink_ids=("s1",),
        ),
    }
    asm = await _assemble(
        monkeypatch,
        points=[make_point("p1", 100), make_point("p2", 101)],
        task_defs=task_defs,
        table=_two_group_table(),
    )
    driver, tasks = asm.driver, asm.tasks
    try:
        await tasks.start_instance("t1:dev1")
        await tasks.start_instance("t2:dev1")
        assert len(driver._listeners) == 2  # noqa: SLF001

        # 重连之后 t1 的监听注册持续被拒；t2 自身始终可以正常注册。
        real_listen = driver.listen
        rejected = {"count": 0}

        async def _listen(point_ids: Any, callback: Any, *, interval: Any = None) -> Any:
            if len(asm.connections) >= 2 and "p1" in point_ids:
                rejected["count"] += 1
                raise RuntimeError("p1 listener registration rejected")
            return await real_listen(point_ids, callback, interval=interval)

        monkeypatch.setattr(driver, "listen", _listen)

        # 用一次派发异常杀死 p2 的 listener 触发 health 恶化与看门狗重连。
        asm.dispatch.failing = True
        asm.connections[0].deliver([floating_data_msg(1, 101, 2.0)])
        await wait_until(
            lambda: True if len(asm.connections) == 2 else None,
            timeout=10.0,
            description="watchdog reconnect triggered by dead listener",
        )
        asm.dispatch.failing = False

        # 给看门狗多个恢复周期（每周期 100ms）：t2 理应被独立恢复。
        deadline = asyncio.get_running_loop().time() + 3.0
        restored = False
        while asyncio.get_running_loop().time() < deadline:
            if "t2:dev1" in tasks._acquisition_handles:  # noqa: SLF001
                restored = True
                break
            await asyncio.sleep(0.05)

        assert restored, "t2 restore must not be starved by persistent t1 restore failure"

        # t2 恢复后再观察多个看门狗周期，验证"恢复未完成"语义：
        t2_handle = tasks._acquisition_handles["t2:dev1"]  # noqa: SLF001
        assert tasks.instance_states()["t2:dev1"] is TaskInstanceState.RUNNING
        count_at_restore = rejected["count"]
        await asyncio.sleep(0.6)  # 约 6 个看门狗周期

        # (4) t1 持续失败但保留 restart_pending 与运行意图（STOPPED 簿记 + pending）。
        assert "t1:dev1" in tasks._restart_pending  # noqa: SLF001
        assert "t1:dev1" in tasks._desired_running  # noqa: SLF001
        assert "t2:dev1" in tasks._desired_running  # noqa: SLF001
        assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED

        # (5)(6) Watchdog 仍在后续周期重试失败的 t1（拒绝计数继续增长）。
        assert rejected["count"] > count_at_restore, "watchdog must keep retrying failed instance"

        # (7) 已恢复的 t2 不重复创建句柄，也不重复发 GI；监听数稳定。
        assert tasks._acquisition_handles["t2:dev1"] is t2_handle  # noqa: SLF001
        assert asm.connections[1].gi_requests == [20]
        listeners = set(driver._listeners)  # noqa: SLF001
        assert len(listeners) == 1  # 只有 t2 的监听存活

        # 持续失败期间不得发生多余的物理重连——恢复重试在既有连接上进行。
        assert len(asm.connections) == 2
    finally:
        await asm.devices.close_all()
        await driver.close()
