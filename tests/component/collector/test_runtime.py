"""新 Collector CollectorRuntime 编排组件测试（内存 Fake 协议/Sink）。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from collector.application.config import CollectionTask
from collector.application.reload import compute_diff
from collector.application.task_instance import TaskInstanceState
from core.domain import DeviceGroupId, DeviceId
from tests.support.new_collector import (
    FakeSink,
    make_collector_config,
    make_runtime,
)


async def test_start_connects_devices_opens_sinks_registers_instances():
    sink = FakeSink()
    config = make_collector_config()
    runtime, proto = make_runtime(config, sinks={"s1": sink})

    await runtime.start()
    assert runtime.running
    assert proto.connect_calls == 1
    assert sink.open_calls == 1
    assert runtime.task_instances()["t1:dev1"].device_id == "dev1"
    assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED

    await runtime.stop()
    assert not runtime.running
    assert sink.close_calls == 1
    assert proto.close_calls == 1


async def test_start_failure_of_one_component_does_not_block_others():
    sink = FakeSink()
    config = make_collector_config()
    runtime, proto = make_runtime(config, sinks={"s1": sink})
    proto.fail_connect = True

    await runtime.start()
    assert runtime.running  # best-effort：设备失败不阻断启动
    health = await runtime.device_health()
    assert not health["dev1"].healthy
    state = runtime.device_state("dev1")
    # 后台 watchdog 可能在断言前再记录一次失败；只要求失败被计入，不锁定次数。
    assert state is not None and state.consecutive_failures >= 1
    await runtime.stop()


async def test_task_instance_collect_flows_to_sink_end_to_end():
    sink = FakeSink()
    config = make_collector_config(
        tasks={
            "t1": CollectionTask(
                task_id="t1",
                device_group_id=DeviceGroupId("dev1"),
                point_group="g",
                interval=0.02,
                sink_ids=("s1",),
            )
        }
    )
    runtime, _ = make_runtime(config, sinks={"s1": sink})
    await runtime.start()
    await runtime.start_task_instance("t1:dev1")

    await asyncio.sleep(0.09)
    await runtime.stop_task_instance("t1:dev1")
    await runtime.stop()

    assert len(sink.batches) >= 1
    assert runtime.points_collected >= 1
    assert runtime.points_routed >= 1
    states = runtime.acquisition_states()
    assert states["t1:dev1"].consecutive_failures == 0


async def test_reconfigure_add_and_remove_device():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    await runtime.start()

    new_config = make_collector_config(device_id="dev2")
    # dev1 → dev2：removed + added
    diff = compute_diff(config, new_config)
    assert diff.devices.removed == ["dev1"] and diff.devices.added == ["dev2"]

    errors = await runtime.reconfigure(new_config, diff)
    assert errors == []
    assert "dev1" not in runtime.devices and "dev2" in runtime.devices
    # dev2 会话由 session_factory 创建并已尝试连接
    view = runtime.device_runtime.view_of("dev2")
    assert view is not None and str(view.device.device_id) == "dev2"
    await runtime.stop()


async def test_reconfigure_lightweight_group_change_keeps_connection():
    config = make_collector_config()
    runtime, proto = make_runtime(config)
    await runtime.start()

    new_config = make_collector_config(device_group_id="wind")
    diff = compute_diff(config, new_config)
    assert diff.devices.updated == ["dev1"]

    errors = await runtime.reconfigure(new_config, diff)
    assert errors == []
    assert proto.connect_calls == 1  # 轻量路径：不重建连接
    assert proto.close_calls == 0
    session = runtime.devices["dev1"]
    assert session.device_group_ids == ("wind",)
    await runtime.stop()


async def test_reconfigure_endpoint_change_rebuilds_device():
    config = make_collector_config()
    runtime, proto = make_runtime(config)
    await runtime.start()
    await runtime.start_task_instance("t1:dev1")

    view = config.device_view(DeviceId("dev1"))
    new_device = replace(view.device, endpoint=replace(view.device.endpoint, port=503))
    new_config = replace(config, devices={DeviceId("dev1"): new_device})
    diff = compute_diff(config, new_config)
    assert diff.devices.updated == ["dev1"]

    errors = await runtime.reconfigure(new_config, diff)
    assert errors == []
    assert proto.close_calls == 1  # 旧连接关闭
    new_session = runtime.devices["dev1"]
    assert new_session.protocol is not proto
    # 原 RUNNING 实例已恢复
    assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    await runtime.stop()


async def test_reconfigure_task_change_updates_instances():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    await runtime.start()
    await runtime.start_task_instance("t1:dev1")

    new_config = make_collector_config(
        tasks={
            "t1": CollectionTask(
                task_id="t1",
                device_group_id=DeviceGroupId("dev1"),
                point_group="g",
                interval=5.0,
                sink_ids=("s1",),
            )
        }
    )
    diff = compute_diff(config, new_config)
    assert diff.tasks.updated == ["t1"]
    errors = await runtime.reconfigure(new_config, diff)
    assert errors == []
    assert runtime.task_instances()["t1:dev1"].interval == 5.0
    assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    await runtime.stop()


async def test_convergence_diff_covers_all_actual_components():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    await runtime.start()

    target = make_collector_config(tasks={})
    diff = runtime.convergence_diff(target)
    assert diff.devices.updated == ["dev1"]  # 保守：交集全部 updated
    assert diff.tasks.removed == ["t1"]
    await runtime.stop()


async def test_stop_idempotent_and_lifecycle_serialized():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    await asyncio.gather(runtime.start(), runtime.start())
    assert runtime.running
    await asyncio.gather(runtime.stop(), runtime.stop())
    assert not runtime.running


async def test_start_failure_releases_devices_and_sinks():
    """Task 实例注册异常时，所有已启动子系统必须回滚。"""
    sink = FakeSink()
    runtime, protocol = make_runtime(make_collector_config(), sinks={"s1": sink})

    async def fail_sync(*, restart_subscription_devices: set[str] | None = None) -> None:
        raise RuntimeError("task sync failed")

    runtime.task_runtime.sync_instances = fail_sync  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="task sync failed"):
        await runtime.start()

    assert not runtime.running
    assert not runtime._running
    assert sink.close_calls == 1
    assert protocol.close_calls == 1


async def test_stop_releases_sinks_and_devices_after_task_stop_failure():
    """停采集失败不能阻止 Sink 和设备释放。"""
    sink = FakeSink()
    runtime, protocol = make_runtime(make_collector_config(), sinks={"s1": sink})
    await runtime.start()

    async def fail_stop() -> None:
        raise RuntimeError("task stop failed")

    runtime.task_runtime.stop_all = fail_stop  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="task stop failed"):
        await runtime.stop()

    assert not runtime.running
    assert sink.close_calls == 1
    assert protocol.close_calls == 1


async def test_startup_error_not_masked_by_cleanup_failure():
    """清理失败不能覆盖实际的启动失败，其他组件仍应释放。"""
    runtime, protocol = make_runtime(make_collector_config())

    async def fail_start(*, restart_subscription_devices: set[str] | None = None) -> None:
        raise RuntimeError("sync failure")

    async def fail_cleanup() -> None:
        raise RuntimeError("sink cleanup failure")

    runtime.task_runtime.sync_instances = fail_start  # type: ignore[method-assign]
    runtime.sink_runtime.stop = fail_cleanup  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="sync failure"):
        await runtime.start()
    assert protocol.close_calls == 1


async def test_shutdown_preserves_first_error_and_still_closes_devices():
    runtime, protocol = make_runtime(make_collector_config())
    await runtime.start()

    async def fail_tasks() -> None:
        raise ValueError("tasks cleanup failed")

    async def fail_sinks() -> None:
        raise RuntimeError("sinks cleanup failed")

    runtime.task_runtime.stop_all = fail_tasks  # type: ignore[method-assign]
    runtime.sink_runtime.stop = fail_sinks  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="tasks cleanup failed"):
        await runtime.stop()
    assert protocol.close_calls == 1
