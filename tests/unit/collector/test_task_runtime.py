"""新 Collector TaskRuntime 实例展开/启停/restart_pending 单元测试。"""

from __future__ import annotations

import pytest

from collector.application.config import CollectionTask, RuntimeParams
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


def _runtime(
    config=None,
    proto: CollectorFakeProtocol | None = None,
) -> tuple[TaskRuntime, DeviceRuntime, CollectorFakeProtocol, AcquisitionEngine]:
    config = config or make_collector_config()
    proto = proto or CollectorFakeProtocol()
    session = make_session(config, proto)
    devices = DeviceRuntime({"dev1": session}, RuntimeParams(connect_timeout=0.2))
    devices.register_view("dev1", config.device_view("dev1"))  # type: ignore[arg-type]
    sink = FakeSink()
    engine = AcquisitionEngine()
    engine.attach_sink_dispatch(_Dispatch(sink))
    engine.attach_device_state(devices)
    tasks = TaskRuntime(dict(config.tasks), devices, engine)
    engine.attach_acquisition_state(tasks)
    tasks._sink = sink  # type: ignore[attr-defined]
    return tasks, devices, proto, engine


class _Dispatch:
    def __init__(self, sink: FakeSink) -> None:
        self.sink = sink

    async def dispatch(self, routed):
        for batch in routed.values():
            await self.sink.write(batch)


async def test_device_task_expands_to_single_instance():
    tasks, _, _, _ = _runtime()
    await tasks.sync_instances()
    instances = tasks.task_instances()
    assert set(instances) == {"t1:dev1"}
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED


async def test_disabled_task_not_expanded():
    config = make_collector_config(
        tasks={
            "t1": CollectionTask(
                task_id="t1",
                device="dev1",
                point_group="g",
                interval=1.0,
                targets=("s1",),
                enabled=False,
            )
        }
    )
    tasks, _, _, _ = _runtime(config)
    await tasks.sync_instances()
    assert tasks.task_instances() == {}


async def test_device_group_task_expands_per_matching_device():
    config = make_collector_config(
        device_group="wind",
        tasks={
            "tg": CollectionTask(
                task_id="tg",
                device_group="wind",
                point_group="g",
                interval=1.0,
                targets=("s1",),
            )
        },
    )
    tasks, _, _, _ = _runtime(config)
    await tasks.sync_instances()
    assert set(tasks.task_instances()) == {"tg:dev1"}


async def test_start_instance_collects_and_dispatches():
    tasks, devices, proto, _ = _runtime()
    await devices.connect_all()
    await tasks.sync_instances()
    await tasks.start_instance("t1:dev1")
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING

    import asyncio

    await asyncio.sleep(0.05)  # interval=1.0 → 首 tick 立即执行一次
    await tasks.stop_instance("t1:dev1")
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
    sink: FakeSink = tasks._sink  # type: ignore[attr-defined]
    assert len(sink.batches) >= 1
    states = tasks.acquisition_states()
    assert states["t1:dev1"].consecutive_failures == 0
    assert states["t1:dev1"].last_success_at is not None


async def test_start_instance_idempotent_and_unknown_raises():
    tasks, _, _, _ = _runtime()
    await tasks.sync_instances()
    await tasks.start_instance("t1:dev1")
    await tasks.start_instance("t1:dev1")  # 幂等
    with pytest.raises(KeyError):
        await tasks.start_instance("ghost")
    with pytest.raises(KeyError):
        await tasks.stop_instance("ghost")
    await tasks.stop_all()


async def test_interval_change_restarts_running_instance():
    tasks, devices, _, _ = _runtime()
    await devices.connect_all()
    await tasks.sync_instances()
    await tasks.start_instance("t1:dev1")
    old_handle = tasks._acquisition_handles["t1:dev1"]

    new_defs = {
        "t1": CollectionTask(
            task_id="t1", device="dev1", point_group="g", interval=2.0, targets=("s1",)
        )
    }
    await tasks.apply_definitions(new_defs)
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    assert tasks._acquisition_handles["t1:dev1"] is not old_handle
    await tasks.stop_all()


async def test_targets_change_does_not_restart_handle():
    tasks, devices, _, _ = _runtime()
    await devices.connect_all()
    await tasks.sync_instances()
    await tasks.start_instance("t1:dev1")
    old_handle = tasks._acquisition_handles["t1:dev1"]

    new_defs = {
        "t1": CollectionTask(
            task_id="t1", device="dev1", point_group="g", interval=1.0, targets=("s2",)
        )
    }
    await tasks.apply_definitions(new_defs)
    assert tasks._acquisition_handles["t1:dev1"] is old_handle
    assert tasks.task_instances()["t1:dev1"].targets == ("s2",)
    await tasks.stop_all()


async def test_restart_failure_marks_pending_and_next_sync_retries():
    config = make_collector_config()
    tasks, devices, proto, _ = _runtime(config)
    await devices.connect_all()
    await tasks.sync_instances()
    await tasks.start_instance("t1:dev1")

    # 订阅式设备让 start_acquisition 失败一次
    session = devices.devices["dev1"]
    original = session.start_acquisition

    async def failing(**kwargs):
        raise RuntimeError("subscribe failed")

    session.start_acquisition = failing  # type: ignore[method-assign]
    new_defs = {
        "t1": CollectionTask(
            task_id="t1", device="dev1", point_group="g", interval=2.0, targets=("s1",)
        )
    }
    with pytest.raises(RuntimeError, match="subscribe failed"):
        await tasks.apply_definitions(new_defs)
    assert "t1:dev1" in tasks._restart_pending
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED

    session.start_acquisition = original  # type: ignore[method-assign]
    await tasks.apply_definitions(new_defs)  # pending → 重试成功
    assert "t1:dev1" not in tasks._restart_pending
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    await tasks.stop_all()


async def test_suspend_and_resume_for_device_change():
    tasks, devices, _, _ = _runtime()
    await devices.connect_all()
    await tasks.sync_instances()
    await tasks.start_instance("t1:dev1")

    was_running = await tasks.suspend_for_device_change("dev1")
    assert was_running == ["t1:dev1"]
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
    assert tasks._acquisition_handles == {}

    await tasks.resume_after_device_change(was_running)
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    await tasks.stop_all()


async def test_stop_all_marks_stopped_and_clears_handles():
    tasks, devices, _, _ = _runtime()
    await devices.connect_all()
    await tasks.sync_instances()
    await tasks.start_instance("t1:dev1")
    await tasks.stop_all()
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
    assert tasks._acquisition_handles == {}
    assert "t1:dev1" in tasks.task_instances()  # 定义保留
