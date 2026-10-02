"""Task / Task Instance 生命周期控制 functional 测试。

真实 Runtime + 真实 Modbus fixture server：验证实例默认 STOPPED、单个/
批量启停、幂等语义与未知/禁用 Task 的错误语义，并确认 RUNNING 实例
确实把采集数据推进 sink。
"""

from __future__ import annotations


import pytest

pytestmark = pytest.mark.modbus
from wind_hub.application.runtime.task_instance import TaskInstanceState
from wind_hub.application.usecase.task import TaskBatchResult

from tests.collector.functional.conftest import DEFAULT_TASK, FunctionalContext
from tests.fixtures.sinks.null_sink import NullSink
from tests.system.wait import wait_until

INSTANCE_ID = "modbus-telemetry:modbus-1"


def _sink(ctx: FunctionalContext) -> NullSink:
    sink = ctx.rt.sinks["null_sink"]
    assert isinstance(sink, NullSink)
    return sink


class TestDefaultState:
    async def test_instances_default_to_stopped(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        instances = await modbus_runtime.rt.tasks.list_instances()
        assert [inst.instance_id for inst in instances] == [INSTANCE_ID]
        assert instances[0].state is TaskInstanceState.STOPPED

    async def test_stopped_instance_collects_nothing(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        # 默认 STOPPED：短暂观察窗口内 sink 不应收到任何点值。
        import asyncio

        await asyncio.sleep(0.6)
        assert _sink(modbus_runtime).received == []


class TestSingleInstanceControl:
    async def test_start_then_stop_instance(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        tasks = modbus_runtime.rt.tasks
        started = await tasks.start_instance(INSTANCE_ID)
        assert started.state is TaskInstanceState.RUNNING

        # RUNNING 实例真实采集：等待 sink 收到点值。
        await wait_until(
            lambda: len(_sink(modbus_runtime).received) >= 1 or None,
            timeout=5.0,
            description="sink receives collected points",
        )

        stopped = await tasks.stop_instance(INSTANCE_ID)
        assert stopped.state is TaskInstanceState.STOPPED

    async def test_start_is_idempotent(self, modbus_runtime: FunctionalContext) -> None:
        tasks = modbus_runtime.rt.tasks
        await tasks.start_instance(INSTANCE_ID)
        again = await tasks.start_instance(INSTANCE_ID)
        assert again.state is TaskInstanceState.RUNNING

    async def test_stop_is_idempotent(self, modbus_runtime: FunctionalContext) -> None:
        tasks = modbus_runtime.rt.tasks
        await tasks.stop_instance(INSTANCE_ID)
        again = await tasks.stop_instance(INSTANCE_ID)
        assert again.state is TaskInstanceState.STOPPED

    async def test_unknown_instance_raises_key_error(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        tasks = modbus_runtime.rt.tasks
        with pytest.raises(KeyError):
            await tasks.start_instance("modbus-telemetry:ghost")
        with pytest.raises(KeyError):
            await tasks.stop_instance("modbus-telemetry:ghost")
        with pytest.raises(KeyError):
            await tasks.get_instance("modbus-telemetry:ghost")


class TestTaskLevelControl:
    async def test_start_and_stop_task(self, modbus_runtime: FunctionalContext) -> None:
        tasks = modbus_runtime.rt.tasks
        summary = await tasks.start_task("modbus-telemetry")
        assert summary.runtime_state == "running"
        assert summary.running_instances == 1

        summary = await tasks.stop_task("modbus-telemetry")
        assert summary.runtime_state == "stopped"
        assert summary.stopped_instances == 1

    async def test_unknown_task_raises_key_error(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        tasks = modbus_runtime.rt.tasks
        with pytest.raises(KeyError):
            await tasks.start_task("ghost-task")
        with pytest.raises(KeyError):
            await tasks.stop_task("ghost-task")
        with pytest.raises(KeyError):
            await tasks.get_task_summary("ghost-task")

    async def test_disabled_task_rejects_start(self, runtime_factory) -> None:
        async with runtime_factory(
            tasks=[{**DEFAULT_TASK, "enabled": False}]
        ) as ctx:
            with pytest.raises(ValueError, match="disabled"):
                await ctx.rt.tasks.start_task("modbus-telemetry")


class TestBatchControl:
    async def test_start_all_then_stop_all(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        tasks = modbus_runtime.rt.tasks
        result = await tasks.start_all_instances()
        assert result == TaskBatchResult(total=1, changed=1, unchanged=0)

        result = await tasks.stop_all_instances()
        assert result == TaskBatchResult(total=1, changed=1, unchanged=0)

    async def test_batch_operations_count_unchanged_instances(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        tasks = modbus_runtime.rt.tasks
        # 实例已 STOPPED：stop-all 全部 unchanged；重复 start-all 同理。
        result = await tasks.stop_all_instances()
        assert result == TaskBatchResult(total=1, changed=0, unchanged=1)

        await tasks.start_all_instances()
        result = await tasks.start_all_instances()
        assert result == TaskBatchResult(total=1, changed=0, unchanged=1)

    async def test_task_summary_reflects_instance_states(
        self, modbus_runtime: FunctionalContext
    ) -> None:
        tasks = modbus_runtime.rt.tasks
        summary = await tasks.get_task_summary("modbus-telemetry")
        assert summary.instance_count == 1
        assert summary.runtime_state == "stopped"

        await tasks.start_all_instances()
        summary = await tasks.get_task_summary("modbus-telemetry")
        assert summary.runtime_state == "running"
        assert summary.running_instances == 1
