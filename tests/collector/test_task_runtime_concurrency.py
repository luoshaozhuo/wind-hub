"""TaskRuntime 启停并发复现（FINDING-TASK-RUNTIME-RACE-01）。

start_instance / stop_instance 之间没有每实例互斥锁，且状态在
``await device.start_acquisition()`` / ``await close()`` 之后才提交，
存在 check-then-await-act 窗口：stop 在 start 的句柄建立期间到达时，
看到的仍是 STOPPED 而提前返回；start 随后把状态置回 RUNNING。

后果（控制面并发 Start/Stop 即可触发，asyncio 单线程不阻止交错）：
stop 正常返回后实例仍在采集，且采集句柄脱离关闭路径泄漏。
"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.config import RuntimeParams
from collector.application.device_runtime import DeviceRuntime
from collector.application.task_instance import TaskInstanceState
from collector.application.task_runtime import TaskRuntime
from collector.domain.acquisition import AcquisitionEngine
from core.domain import DeviceGroupId
from tests.support.new_collector import (
    CollectionTask,
    CollectorFakeProtocol,
    make_collector_config,
    make_session,
)

#: v2 每设备一个 Task，task_id == device_id == instance_id。
DEVICE_TASKS = {
    "dev1": CollectionTask(
        task_id="dev1",
        device_group_id=DeviceGroupId("dev1"),
        point_group="g",
        interval=1.0,
    )
}

pytestmark = pytest.mark.unit

_RUNTIMES: list[DeviceRuntime] = []


@pytest.fixture(autouse=True)
async def _close_device_runtimes():
    yield
    for devices in _RUNTIMES:
        await devices.close_all()
    _RUNTIMES.clear()


def _build():
    config = make_collector_config(tasks=DEVICE_TASKS)
    proto = CollectorFakeProtocol()
    session = make_session(config, proto)
    devices = DeviceRuntime({"dev1": session}, RuntimeParams(connect_timeout=0.2))
    _RUNTIMES.append(devices)
    engine = AcquisitionEngine()

    class _NullDispatch:
        """并发语义测试不关心发布结果：绑定空 NATS 派发端口即可。"""

        async def dispatch(self, task_id, values):
            del task_id, values

    engine.attach_sample_publisher(_NullDispatch())
    engine.attach_device_state(devices)
    tasks = TaskRuntime(dict(config.tasks), devices, engine)
    engine.attach_acquisition_state(tasks)
    return tasks, session, devices


async def test_concurrent_stop_during_start_cannot_leave_running() -> None:
    """stop 必须对进行中的 start 生效：返回后实例 STOPPED、无残留句柄。"""
    tasks, session, devices = _build()
    await devices.connect_all()
    await tasks.sync_instances()
    instance_id = "dev1"

    gate = asyncio.Event()
    original_start = session.start_acquisition

    async def gated_start(**kwargs):
        await gate.wait()
        return await original_start(**kwargs)

    session.start_acquisition = gated_start

    start_task = asyncio.create_task(tasks.start_instance(instance_id))
    # 让 start 进入挂起的 start_acquisition（状态尚未提交为 RUNNING）。
    await asyncio.sleep(0.05)
    stop_task = asyncio.create_task(tasks.stop_instance(instance_id))
    # 让 stop 在 start 提交状态前完成检查。
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(start_task, stop_task)

    state = tasks.instance_states()[instance_id]
    assert (
        state is TaskInstanceState.STOPPED
    ), f"stop 返回后实例仍为 {state}——启停未互斥，stop 被 start 覆盖"
    assert (
        tasks._acquisition_handles.get(instance_id) is None  # noqa: SLF001
    ), "start 建立的采集句柄脱离关闭路径，发生泄漏"


async def test_concurrent_double_start_creates_single_handle() -> None:
    """Start/Start：同一实例并发启动只允许建立一个采集句柄。"""
    tasks, session, _devices = _build()
    await _devices.connect_all()
    await tasks.sync_instances()
    instance_id = "dev1"

    gate = asyncio.Event()
    original_start = session.start_acquisition
    calls = 0

    async def gated_start(**kwargs):
        nonlocal calls
        calls += 1
        await gate.wait()
        return await original_start(**kwargs)

    session.start_acquisition = gated_start
    starts = [asyncio.create_task(tasks.start_instance(instance_id)) for _ in range(2)]
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(*starts)

    assert calls == 1
    assert tasks.instance_states()[instance_id] is TaskInstanceState.RUNNING
    assert instance_id in tasks._acquisition_handles  # noqa: SLF001


async def test_concurrent_double_stop_is_idempotent() -> None:
    """Stop/Stop：重复停止不触碰已关闭句柄，最终 STOPPED。"""
    tasks, session, devices = _build()
    await devices.connect_all()
    await tasks.sync_instances()
    instance_id = "dev1"
    await tasks.start_instance(instance_id)
    handle = tasks._acquisition_handles[instance_id]  # noqa: SLF001

    await asyncio.gather(tasks.stop_instance(instance_id), tasks.stop_instance(instance_id))

    assert tasks.instance_states()[instance_id] is TaskInstanceState.STOPPED
    assert instance_id not in tasks._acquisition_handles  # noqa: SLF001
    assert handle._closed is True  # type: ignore[attr-defined]  # noqa: SLF001


async def test_start_failure_during_pending_stop_leaves_stopped() -> None:
    """start 建句柄失败：并发 stop 在场时最终仍为 STOPPED，无句柄残留。"""
    tasks, session, devices = _build()
    await devices.connect_all()
    await tasks.sync_instances()
    instance_id = "dev1"

    gate = asyncio.Event()

    async def failing_start(**kwargs):
        await gate.wait()
        raise RuntimeError("subscribe rejected")

    session.start_acquisition = failing_start
    start_task = asyncio.create_task(tasks.start_instance(instance_id))
    await asyncio.sleep(0.05)
    stop_task = asyncio.create_task(tasks.stop_instance(instance_id))
    gate.set()
    with pytest.raises(RuntimeError, match="subscribe rejected"):
        await start_task
    await stop_task

    assert tasks.instance_states()[instance_id] is TaskInstanceState.STOPPED
    assert instance_id not in tasks._acquisition_handles  # noqa: SLF001


async def test_repeated_concurrent_start_stop_rounds_converge() -> None:
    """20 轮乱序并发 Start/Stop 后必须收敛：STOPPED、无句柄、无遗留 task。"""
    tasks, _session, devices = _build()
    await devices.connect_all()
    await tasks.sync_instances()
    instance_id = "dev1"

    for round_index in range(20):
        if round_index % 2 == 0:
            await asyncio.gather(
                tasks.start_instance(instance_id),
                tasks.stop_instance(instance_id),
            )
        else:
            await asyncio.gather(
                tasks.stop_instance(instance_id),
                tasks.start_instance(instance_id),
            )
            # 后半轮先停后启，显式收敛到停止。
            await tasks.stop_instance(instance_id)

    assert tasks.instance_states()[instance_id] is TaskInstanceState.STOPPED
    assert tasks._acquisition_handles == {}  # noqa: SLF001
    assert tasks._operation_locks  # noqa: SLF001  # 实例仍注册，锁保留


async def test_operation_lock_reclaimed_after_instance_unregister_and_recreate() -> None:
    """实例注销后锁回收；同名重建后的新实例与在途调用不会分裂成两把锁。"""
    tasks, _session, devices = _build()
    await devices.connect_all()
    await tasks.sync_instances()
    instance_id = "dev1"
    await tasks.start_instance(instance_id)
    await tasks.stop_instance(instance_id)

    await tasks.apply_definitions({})
    assert instance_id not in tasks.task_instances()
    assert tasks._operation_locks == {}  # noqa: SLF001 — 注销即回收，无无限增长

    config = make_collector_config(tasks=DEVICE_TASKS)
    await tasks.apply_definitions(dict(config.tasks))
    assert instance_id in tasks.task_instances()
    await tasks.start_instance(instance_id)
    assert tasks.instance_states()[instance_id] is TaskInstanceState.RUNNING
    assert instance_id in tasks._operation_locks  # noqa: SLF001
    await tasks.stop_instance(instance_id)
    assert tasks.instance_states()[instance_id] is TaskInstanceState.STOPPED
