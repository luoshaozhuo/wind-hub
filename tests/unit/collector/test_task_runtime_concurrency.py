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
from tests.support.new_collector import (
    CollectorFakeProtocol,
    FakeSink,
    make_collector_config,
    make_session,
)

_RUNTIMES: list[DeviceRuntime] = []


@pytest.fixture(autouse=True)
async def _close_device_runtimes():
    yield
    for devices in _RUNTIMES:
        await devices.close_all()
    _RUNTIMES.clear()


def _build():
    config = make_collector_config()
    proto = CollectorFakeProtocol()
    session = make_session(config, proto)
    devices = DeviceRuntime({"dev1": session}, RuntimeParams(connect_timeout=0.2))
    _RUNTIMES.append(devices)
    sink = FakeSink()
    engine = AcquisitionEngine()

    class _Dispatch:
        async def dispatch(self, routed):
            for batch in routed.values():
                await sink.write(batch)

    engine.attach_sink_dispatch(_Dispatch())
    engine.attach_device_state(devices)
    tasks = TaskRuntime(dict(config.tasks), devices, engine)
    engine.attach_acquisition_state(tasks)
    return tasks, session, devices


async def test_concurrent_stop_during_start_cannot_leave_running() -> None:
    """stop 必须对进行中的 start 生效：返回后实例 STOPPED、无残留句柄。"""
    tasks, session, devices = _build()
    await devices.connect_all()
    await tasks.sync_instances()
    instance_id = "t1:dev1"

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
