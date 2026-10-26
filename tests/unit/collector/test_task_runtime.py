"""新 Collector TaskRuntime 实例展开/启停/restart_pending 单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.config import CollectionTask, RuntimeParams
from collector.application.device_runtime import DeviceRuntime
from collector.application.task_instance import TaskInstanceState
from collector.application.task_runtime import TaskRuntime
from collector.domain.acquisition import AcquisitionEngine
from core.domain.identities import DeviceGroupId
from tests.support.new_collector import (
    CollectorFakeProtocol,
    FakeSink,
    make_collector_config,
    make_session,
)

_RUNTIMES: list[DeviceRuntime] = []


@pytest.fixture(autouse=True)
async def _close_device_runtimes():
    """connect_all 会启动 Watchdog 后台任务；用例结束统一关闭，防止任务随 GC 销毁。"""
    yield
    for devices in _RUNTIMES:
        await devices.close_all()
    _RUNTIMES.clear()


def _runtime(
    config=None,
    proto: CollectorFakeProtocol | None = None,
) -> tuple[TaskRuntime, DeviceRuntime, CollectorFakeProtocol, AcquisitionEngine]:
    config = config or make_collector_config()
    proto = proto or CollectorFakeProtocol()
    session = make_session(config, proto)
    devices = DeviceRuntime({"dev1": session}, RuntimeParams(connect_timeout=0.2))
    _RUNTIMES.append(devices)
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
        for delivery in routed.values():
            await self.sink.write(delivery.values)


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
                device_group_id=DeviceGroupId("dev1"),
                point_group="g",
                interval=1.0,
                sink_ids=("s1",),
                enabled=False,
            )
        }
    )
    tasks, _, _, _ = _runtime(config)
    await tasks.sync_instances()
    assert tasks.task_instances() == {}


async def test_device_group_task_expands_per_matching_device():
    config = make_collector_config(
        device_group_id="wind",
        tasks={
            "tg": CollectionTask(
                task_id="tg",
                device_group_id=DeviceGroupId("wind"),
                point_group="g",
                interval=1.0,
                sink_ids=("s1",),
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
            task_id="t1",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g",
            interval=2.0,
            sink_ids=("s1",),
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
            task_id="t1",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g",
            interval=1.0,
            sink_ids=("s2",),
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
            task_id="t1",
            device_group_id=DeviceGroupId("dev1"),
            point_group="g",
            interval=2.0,
            sink_ids=("s1",),
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


# ---------------------------------------------------------------------------
# N1（round4）：Watchdog 恢复按实例隔离
# ---------------------------------------------------------------------------


def _two_group_tasks() -> dict[str, CollectionTask]:
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
            point_group="g2",
            interval=1.0,
            sink_ids=("s1",),
        ),
    }


async def test_start_device_tasks_isolates_failed_instance_and_signals_incomplete():
    """t1 LISTEN 持续失败：t2 仍恢复；t1 保留 pending；方法上抛让 Watchdog 知道
    恢复未完成；下一轮只重试 t1，t2 不重复建句柄；两实例运行意图均保留。"""
    from core.application import ProtocolCapability

    config = make_collector_config(tasks=_two_group_tasks())
    proto = CollectorFakeProtocol()
    proto.capabilities_value = proto.capabilities_value | {ProtocolCapability.LISTEN}
    tasks, devices, _, _ = _runtime(config, proto)
    await tasks.sync_instances()

    # 模拟 Watchdog 恢复入口：两实例都是 desired_running 但当前 STOPPED。
    tasks._desired_running.update({"t1:dev1", "t2:dev1"})

    real_listen = proto.listen
    t1_attempts = {"count": 0}

    async def _listen(point_ids, callback, *, interval=None):
        # t1 选点组 g → ["p1"]；t2 的 g2 无点 → []。
        if list(point_ids) == ["p1"]:
            t1_attempts["count"] += 1
            raise RuntimeError("listen registration rejected")
        return await real_listen(point_ids, callback, interval=interval)

    proto.listen = _listen  # type: ignore[method-assign]

    # 恢复"未完成"必须以异常通知 Watchdog（pending restore 不被清除、下轮重试）。
    with pytest.raises(RuntimeError, match="pending acquisition restores"):
        await tasks.start_device_tasks("dev1", can_restore=lambda: True)

    # t2 独立恢复成功；t1 保持 STOPPED + pending，两实例运行意图都保留。
    assert tasks.instance_states()["t2:dev1"] is TaskInstanceState.RUNNING
    assert "t2:dev1" in tasks._acquisition_handles
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
    assert "t1:dev1" in tasks._restart_pending
    assert {"t1:dev1", "t2:dev1"} <= tasks._desired_running

    t2_handle = tasks._acquisition_handles["t2:dev1"]
    successful_listens = len(proto.listeners)

    # 下一轮恢复：仍上抛"未完成"，t1 被再次重试，t2 跳过（RUNNING 不重建）。
    with pytest.raises(RuntimeError, match="pending acquisition restores"):
        await tasks.start_device_tasks("dev1", can_restore=lambda: True)
    assert t1_attempts["count"] == 2
    assert tasks._acquisition_handles["t2:dev1"] is t2_handle
    assert len(proto.listeners) == successful_listens

    # t1 恢复成功后 pending 清除，且不再上抛。
    async def _recover(point_ids, callback, *, interval=None):
        return await real_listen(point_ids, callback, interval=interval)

    proto.listen = _recover  # type: ignore[method-assign]
    await tasks.start_device_tasks("dev1", can_restore=lambda: True)
    assert "t1:dev1" not in tasks._restart_pending
    assert tasks.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    await tasks.stop_all()


async def test_start_device_tasks_cancelled_instance_propagates_cancellation():
    """恢复期间收到 CancelledError 必须正常传播，不得被逐实例 except 吞掉。"""
    from core.application import ProtocolCapability

    config = make_collector_config(tasks=_two_group_tasks())
    proto = CollectorFakeProtocol()
    proto.capabilities_value = proto.capabilities_value | {ProtocolCapability.LISTEN}
    tasks, devices, _, _ = _runtime(config, proto)
    await tasks.sync_instances()
    tasks._desired_running.update({"t1:dev1", "t2:dev1"})

    async def _cancelled_listen(point_ids, callback, *, interval=None):
        raise asyncio.CancelledError

    proto.listen = _cancelled_listen  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await tasks.start_device_tasks("dev1", can_restore=lambda: True)

    # 取消传播意味着恢复循环立即中止：t2 未被启动。
    assert "t2:dev1" not in tasks._acquisition_handles
    await tasks.stop_all()
