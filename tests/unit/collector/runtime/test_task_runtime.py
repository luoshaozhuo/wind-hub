"""TaskRuntime ownership、实例启停与设备重建恢复的直接验证。

使用真实 CollectorDeviceSession/DeviceRuntime/TaskRuntime 与 mock ProtocolPort；
PollingAcquisitionHandle 为真实 fixed-rate 协程，引擎用内存替身只记录调用。
端到端展开规则、热重载 diff 与 polling 节拍行为仍由 test_runtime.py 覆盖。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.application.runtime import CollectorDeviceSession, CollectorRuntime
from wind_hub_collector.application.runtime.device_runtime import DeviceRuntime
from wind_hub_collector.application.runtime.task_instance import TaskInstanceState
from wind_hub_collector.application.runtime.task_runtime import TaskRuntime
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config.schema import (
    CollectionTaskConfig,
    DeviceConfig,
    RuntimeConfig,
    TaskTarget,
)
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue
from wind_hub_core.protocol.port import AcquisitionMode, ProtocolPort


def _device_config(device_id: str = "d1") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol="modbus",
        point_table="t1",
        endpoint=Endpoint(host="127.0.0.1", port=502),
    )


def _protocol() -> MagicMock:
    protocol = MagicMock(spec=ProtocolPort)
    protocol.connect = AsyncMock()
    protocol.close = AsyncMock()
    protocol.health.return_value = HealthStatus(healthy=True)
    protocol.acquisition_mode = AcquisitionMode.POLL
    return protocol


def _task(task_id: str = "t1", device: str = "d1", interval: float = 0.02) -> CollectionTaskConfig:
    return CollectionTaskConfig(
        task_id=task_id,
        device=device,
        point_group="g1",
        interval=interval,
        targets=[TaskTarget(sink="s1")],
    )


class _FakeEngine:
    """AcquisitionEngine 的内存替身——TaskRuntime 只依赖 collect/process 两面。"""

    def __init__(self) -> None:
        self.collect_calls: list[str] = []
        self.process_calls: list[list[PointValue]] = []

    async def collect(
        self,
        device: CollectorDeviceSession,
        point_group: str,
        targets: list[str],
        execution_id: str,
    ) -> None:
        self.collect_calls.append(execution_id)

    async def process(self, batch: list[PointValue], targets: list[str]) -> None:
        self.process_calls.append(batch)


def _build(
    tasks: list[CollectionTaskConfig] | None = None,
) -> tuple[TaskRuntime, DeviceRuntime, MagicMock, _FakeEngine]:
    protocol = _protocol()
    devices = DeviceRuntime(
        {"d1": CollectorDeviceSession(_device_config(), [], protocol)}, RuntimeConfig()
    )
    engine = _FakeEngine()
    runtime = TaskRuntime(
        {t.task_id: t for t in tasks or [_task()]},
        devices,
        engine,  # type: ignore[arg-type]  # 鸭子类型替身，仅实现 TaskRuntime 依赖面
    )
    return runtime, devices, protocol, engine


def test_collector_transfers_task_registries_and_engine_port_to_one_owner() -> None:
    collector = CollectorRuntime(
        devices={"d1": CollectorDeviceSession(_device_config(), [], _protocol())},
        sinks={},
        engine=AcquisitionEngine(),
        config=RuntimeConfig(),
        tasks={"t1": _task()},
    )

    tasks = collector.task_runtime
    assert isinstance(tasks, TaskRuntime)
    assert tasks.task_definitions().keys() == {"t1"}
    # 六类簿记的唯一权威在 TaskRuntime，CollectorRuntime 不保留任何副本。
    assert not {
        "_task_defs",
        "_task_instances",
        "_instance_states",
        "_acquisition_handles",
        "_acquisition_states",
        "_restart_pending",
    } & vars(collector).keys()
    # 引擎直接绑定采集状态 owner，CollectorRuntime 不再实现该端口。
    assert collector.engine._acquisition_state is tasks
    # 既有 public 查询面是同一来源的只读视图。
    assert collector.task_definitions() == tasks.task_definitions()
    assert collector.task_instances() == tasks.task_instances()
    assert collector.instance_states() == tasks.instance_states()
    assert collector.acquisition_states() == tasks.acquisition_states()


async def test_start_stop_instance_handle_lifecycle_and_idempotency() -> None:
    runtime, devices, _, engine = _build()
    await devices.connect_all()
    await runtime.sync_instances()
    try:
        assert runtime.instance_states() == {"t1:d1": TaskInstanceState.STOPPED}
        assert runtime.task_instances()["t1:d1"].task_id == "t1"
        # 注册即建立独立的采集执行状态（第三维度，初始无失败）。
        acq = runtime.acquisition_states()["t1:d1"]
        assert acq.consecutive_failures == 0 and acq.running is False

        await runtime.start_instance("t1:d1")
        first = runtime._acquisition_handles["t1:d1"]  # noqa: SLF001
        await runtime.start_instance("t1:d1")  # 幂等——同一实例绝不出现两份句柄
        assert runtime._acquisition_handles["t1:d1"] is first  # noqa: SLF001
        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
        # POLL 句柄按节拍立即驱动首轮 collect。
        for _ in range(100):
            if engine.collect_calls:
                break
            await asyncio.sleep(0.005)
        assert engine.collect_calls

        await runtime.stop_instance("t1:d1")
        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
        assert runtime._acquisition_handles == {}  # noqa: SLF001
        await runtime.stop_instance("t1:d1")  # 已 STOPPED——幂等不抛、不重复 close
        # 采集执行状态不随启停清除——失败历史与生命周期分维度。
        assert runtime.acquisition_states()["t1:d1"] is acq
    finally:
        await runtime.stop_all()
        await devices.close_all()


async def test_suspend_resume_around_device_rebuild_restores_only_running() -> None:
    runtime, devices, protocol, _ = _build(
        tasks=[_task("t1"), _task("t2", interval=0.05)]
    )
    await devices.connect_all()
    await runtime.sync_instances()
    try:
        await runtime.start_instance("t1:d1")  # t2:d1 保持 STOPPED
        old_handle = runtime._acquisition_handles["t1:d1"]  # noqa: SLF001

        was_running = await runtime.suspend_for_device_change("d1")

        assert was_running == ["t1:d1"]
        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
        assert runtime._acquisition_handles == {}  # noqa: SLF001

        new_protocol = _protocol()
        await devices.rebuild_device("d1", _device_config(), new_protocol, [])
        await runtime.sync_instances()
        await runtime.resume_after_device_change(was_running)

        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
        assert runtime._acquisition_handles["t1:d1"] is not old_handle  # noqa: SLF001
        # 原本 STOPPED 的实例不被恢复。
        assert runtime.instance_states()["t2:d1"] is TaskInstanceState.STOPPED
        assert set(runtime._acquisition_handles) == {"t1:d1"}  # noqa: SLF001
        protocol.close.assert_awaited_once()
    finally:
        await runtime.stop_all()
        await devices.close_all()
    new_protocol.close.assert_awaited_once()


async def test_restart_pending_retained_on_failed_restart_and_recovered_next_sync() -> None:
    runtime, devices, _, _ = _build()
    await devices.connect_all()
    await runtime.sync_instances()
    try:
        await runtime.start_instance("t1:d1")
        device = devices.devices["d1"]
        real_start = device.start_acquisition
        device.start_acquisition = AsyncMock(side_effect=RuntimeError("register failed"))  # type: ignore[method-assign]

        # interval 变化触发句柄重建；重建失败 → pending 保留、状态 STOPPED、错误上抛。
        with pytest.raises(RuntimeError, match="register failed"):
            await runtime.apply_definitions({"t1": _task(interval=0.05)})
        assert "t1:d1" in runtime._restart_pending  # noqa: SLF001
        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
        assert runtime._acquisition_handles == {}  # noqa: SLF001

        # 后续 reload 重放同一目标：pending 驱动恢复，成功后清除。
        device.start_acquisition = real_start  # type: ignore[method-assign]
        await runtime.apply_definitions({"t1": _task(interval=0.05)})
        assert "t1:d1" not in runtime._restart_pending  # noqa: SLF001
        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
        assert set(runtime._acquisition_handles) == {"t1:d1"}  # noqa: SLF001
    finally:
        await runtime.stop_all()
        await devices.close_all()


async def test_instance_removal_leaves_no_orphan_handle_state_or_pending() -> None:
    runtime, devices, _, _ = _build()
    await devices.connect_all()
    await runtime.sync_instances()
    try:
        await runtime.start_instance("t1:d1")
        handle = runtime._acquisition_handles["t1:d1"]  # noqa: SLF001
        runtime.report_collect_failure("t1:d1", "d1", "g1", "read boom")
        assert runtime.acquisition_states()["t1:d1"].consecutive_failures == 1

        await runtime.apply_definitions({})

        assert runtime.task_instances() == {}
        assert runtime.instance_states() == {}
        assert runtime.acquisition_states() == {}
        assert runtime._acquisition_handles == {}  # noqa: SLF001
        assert runtime._restart_pending == set()  # noqa: SLF001
        await handle.close()  # 已关闭句柄重复 close 幂等（无 orphan 协程残留）
    finally:
        await runtime.stop_all()
        await devices.close_all()


async def test_acquisition_state_stays_separate_from_instance_lifecycle() -> None:
    runtime, devices, _, _ = _build()
    await devices.connect_all()
    await runtime.sync_instances()
    try:
        await runtime.start_instance("t1:d1")
        runtime.report_collect_started("t1:d1", "d1", "g1")
        state = runtime.acquisition_states()["t1:d1"]
        assert state.running is True
        runtime.report_collect_failure("t1:d1", "d1", "g1", "read boom")
        assert state.running is False
        assert state.consecutive_failures == 1
        assert state.last_error == "read boom"
        # 采集失败不改变实例生命周期状态。
        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.RUNNING
        runtime.report_collect_success("t1:d1", "d1", "g1", partial=True)
        assert state.consecutive_failures == 0
        assert state.last_duration is not None

        await runtime.stop_all()
        # 停机只翻转生命周期状态，执行状态簿保留。
        assert runtime.instance_states()["t1:d1"] is TaskInstanceState.STOPPED
        assert runtime._acquisition_handles == {}  # noqa: SLF001
        assert runtime.acquisition_states()["t1:d1"] is state
    finally:
        await devices.close_all()
