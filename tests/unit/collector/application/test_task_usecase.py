"""TaskUseCase 的单元测试。

验证对象：``application/usecase/task.py``——采集 Task / Task Instance
显式生命周期的应用编排。

Runtime 用最小 Fake（按 ``TaskUseCase`` 实际调用的五个方法桩出真实状态
迁移：``task_definitions`` / ``task_instances`` / ``instance_states`` /
``start_task_instance`` / ``stop_task_instance``）——不做 fake OK：
每个断言都落到 Fake 簿记的真实状态翻转与调用记录上。

覆盖点：

- ``list_instances`` / ``get_instance``：实例定义 + 生命周期状态合并；
- 未知 ``instance_id`` → ``KeyError``（get / start / stop）；
- ``start_instance`` / ``stop_instance`` 幂等——重复调用状态稳定；
"""

from __future__ import annotations

import pytest

from wind_hub_collector.application.runtime.task_instance import (
    CollectionTaskInstance,
    TaskInstanceState,
)
from wind_hub_collector.application.usecase.task import TaskUseCase
from wind_hub_core.config.schema import CollectionTaskConfig, TaskTarget

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 最小 Fake Runtime——只实现 TaskUseCase 实际调用的方法，状态迁移真实簿记
# ---------------------------------------------------------------------------


class _FakeCollectorRuntime:
    """按 TaskUseCase 调用面做的最小 Runtime 桩。

    与生产 Runtime 一致的状态语义：start/stop 翻转 ``_states`` 簿记；
    未知 instance_id 抛 ``KeyError``；调用记录用于断言批量操作不触碰
    已在目标状态的实例。
    """

    def __init__(
        self,
        tasks: dict[str, CollectionTaskConfig],
        instances: dict[str, CollectionTaskInstance],
        initial_states: dict[str, TaskInstanceState] | None = None,
    ) -> None:
        self._tasks = dict(tasks)
        self._instances = dict(instances)
        initial_states = initial_states or {}
        self._states = {
            iid: initial_states.get(iid, TaskInstanceState.STOPPED) for iid in instances
        }
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []

    def task_definitions(self) -> dict[str, CollectionTaskConfig]:
        return dict(self._tasks)

    def task_instances(self) -> dict[str, CollectionTaskInstance]:
        return dict(self._instances)

    def instance_states(self) -> dict[str, TaskInstanceState]:
        return dict(self._states)

    def acquisition_states(self) -> dict[str, object]:
        return {}

    async def start_task_instance(self, instance_id: str) -> None:
        if instance_id not in self._instances:
            raise KeyError(instance_id)
        self.start_calls.append(instance_id)
        self._states[instance_id] = TaskInstanceState.RUNNING

    async def stop_task_instance(self, instance_id: str) -> None:
        if instance_id not in self._instances:
            raise KeyError(instance_id)
        self.stop_calls.append(instance_id)
        self._states[instance_id] = TaskInstanceState.STOPPED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str = "t1",
    device: str | None = "dev-a",
    device_group: str | None = None,
    interval: float = 1.0,
    sinks: list[str] | None = None,
    enabled: bool = True,
) -> CollectionTaskConfig:
    return CollectionTaskConfig(
        task_id=task_id,
        device=device,
        device_group=device_group,
        point_group="fast",
        interval=interval,
        targets=[TaskTarget(sink=s) for s in (sinks or ["s1"])],
        enabled=enabled,
    )


def _instance(
    task_id: str = "t1",
    device_id: str = "dev-a",
    interval: float = 1.0,
    targets: list[str] | None = None,
) -> CollectionTaskInstance:
    return CollectionTaskInstance(
        instance_id=f"{task_id}:{device_id}",
        task_id=task_id,
        device_id=device_id,
        point_group="fast",
        interval=interval,
        targets=targets or ["s1"],
    )


def _usecase(
    *,
    initial_states: dict[str, TaskInstanceState] | None = None,
) -> tuple[TaskUseCase, _FakeCollectorRuntime]:
    tasks = {
        "t1": _task("t1", device="dev-a", sinks=["s1", "s2"]),
        "t2": _task("t2", device=None, device_group="turbine", interval=5.0, enabled=False),
    }
    instances = {
        "t1:dev-a": _instance("t1", "dev-a", targets=["s1", "s2"]),
        "t2:dev-b": _instance("t2", "dev-b", interval=5.0),
    }
    runtime = _FakeCollectorRuntime(tasks, instances, initial_states)
    return TaskUseCase(runtime), runtime


# ---------------------------------------------------------------------------
# list_instances / get_instance
# ---------------------------------------------------------------------------


async def test_list_instances_merges_definition_and_state() -> None:
    usecase, _ = _usecase(initial_states={"t1:dev-a": TaskInstanceState.RUNNING})

    details = await usecase.list_instances()

    by_id = {d.instance_id: d for d in details}
    assert set(by_id) == {"t1:dev-a", "t2:dev-b"}
    d1 = by_id["t1:dev-a"]
    assert d1.task_id == "t1"
    assert d1.device_id == "dev-a"
    assert d1.point_group == "fast"
    assert d1.interval == 1.0
    assert d1.targets == ["s1", "s2"]
    assert d1.state is TaskInstanceState.RUNNING
    assert by_id["t2:dev-b"].state is TaskInstanceState.STOPPED


async def test_get_instance_returns_single_detail() -> None:
    usecase, _ = _usecase()

    detail = await usecase.get_instance("t2:dev-b")

    assert detail.instance_id == "t2:dev-b"
    assert detail.task_id == "t2"
    assert detail.state is TaskInstanceState.STOPPED


async def test_get_instance_unknown_raises_key_error() -> None:
    usecase, _ = _usecase()

    with pytest.raises(KeyError):
        await usecase.get_instance("nope:dev-x")


# ---------------------------------------------------------------------------
# start_instance / stop_instance
# ---------------------------------------------------------------------------


async def test_start_instance_flips_state_to_running() -> None:
    usecase, runtime = _usecase()

    detail = await usecase.start_instance("t1:dev-a")

    assert detail.state is TaskInstanceState.RUNNING
    assert runtime.instance_states()["t1:dev-a"] is TaskInstanceState.RUNNING
    assert runtime.start_calls == ["t1:dev-a"]


async def test_start_instance_is_idempotent() -> None:
    usecase, runtime = _usecase(initial_states={"t1:dev-a": TaskInstanceState.RUNNING})

    detail = await usecase.start_instance("t1:dev-a")

    assert detail.state is TaskInstanceState.RUNNING
    assert runtime.instance_states()["t1:dev-a"] is TaskInstanceState.RUNNING


async def test_start_instance_unknown_raises_key_error() -> None:
    usecase, _ = _usecase()

    with pytest.raises(KeyError):
        await usecase.start_instance("nope:dev-x")


async def test_stop_instance_flips_state_to_stopped() -> None:
    usecase, runtime = _usecase(initial_states={"t1:dev-a": TaskInstanceState.RUNNING})

    detail = await usecase.stop_instance("t1:dev-a")

    assert detail.state is TaskInstanceState.STOPPED
    assert runtime.instance_states()["t1:dev-a"] is TaskInstanceState.STOPPED
    assert runtime.stop_calls == ["t1:dev-a"]


async def test_stop_instance_is_idempotent() -> None:
    usecase, runtime = _usecase()

    detail = await usecase.stop_instance("t1:dev-a")

    assert detail.state is TaskInstanceState.STOPPED
    assert runtime.instance_states()["t1:dev-a"] is TaskInstanceState.STOPPED


async def test_stop_instance_unknown_raises_key_error() -> None:
    usecase, _ = _usecase()

    with pytest.raises(KeyError):
        await usecase.stop_instance("nope:dev-x")


# ---------------------------------------------------------------------------
# V1 Task aggregate operations
# ---------------------------------------------------------------------------


async def test_task_summary_reports_running_instances() -> None:
    """聚合状态应保持生命周期与采集失败两个维度分离。"""
    usecase, _ = _usecase(initial_states={"t1:dev-a": TaskInstanceState.RUNNING})

    summary = await usecase.get_task_summary("t1")

    assert summary.runtime_state == "running"
    assert summary.instance_count == 1
    assert summary.running_instances == 1
    assert summary.failed_instances == 0


async def test_start_task_rejects_disabled_definition() -> None:
    """禁用 Task 不允许通过 Task 级入口启动。"""
    usecase, runtime = _usecase()

    with pytest.raises(ValueError, match="disabled"):
        await usecase.start_task("t2")

    assert runtime.start_calls == []


async def test_start_and_stop_task_operate_only_its_instances() -> None:
    """Task 级启停只影响该定义展开出的实例。"""
    usecase, runtime = _usecase()

    started = await usecase.start_task("t1")
    stopped = await usecase.stop_task("t1")

    assert started.runtime_state == "running"
    assert stopped.runtime_state == "stopped"
    assert runtime.start_calls == ["t1:dev-a"]
    assert runtime.stop_calls == ["t1:dev-a"]
