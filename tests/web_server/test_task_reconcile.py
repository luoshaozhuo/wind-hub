"""TaskPlacementReconciler 收敛与 placement safety 单元测试（v2 语义）。

Collector 不再接受 placement 快照/代次确认（v1 的 ApplyTaskPlacement 与
generation/task_count ack 已移除）；Server 侧收敛只做两件事：停掉跑在错误
Worker 上的 running 实例、按 Worker 可用性/孤儿 placement 决定 safety 栅栏。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from web_server.application.port.worker import (
    CollectorInfo,
    CollectorTaskInstance,
)
from web_server.application.task.placement import TaskPlacementRegistry
from web_server.application.task.reconcile import (
    TaskPlacementReconciler,
    TaskPlacementUnsafeError,
)

pytestmark = pytest.mark.unit


class _Config:
    def __init__(self, config_dir, task_ids: list[str]) -> None:
        self.config_dir = config_dir
        self.current_config = SimpleNamespace(
            tasks={
                task_id: SimpleNamespace(task_id=task_id, device_group_id="d1", enabled=True)
                for task_id in task_ids
            },
            # v2 reconcile 按 device_group_id→设备集合归属；实例 fixture 的
            # device_id 为 "d1"，隶属同名设备组且启用。
            site=SimpleNamespace(
                devices={
                    "d1": SimpleNamespace(device_id="d1", device_group_ids=("d1",), enabled=True)
                }
            ),
        )


class _Collector:
    def __init__(self, worker_id: str, instances: list[CollectorTaskInstance]) -> None:
        self.worker_id = worker_id
        self.instances = list(instances)
        self.stopped: list[str] = []
        self.reported_id = worker_id
        self.unavailable = False

    async def config_status(self) -> CollectorInfo:
        if self.unavailable:
            raise ConnectionError("unreachable")
        return CollectorInfo(
            collector_id=self.reported_id,
            boot_id="boot-1",
            active_revision="rev-1",
            runtime_running=True,
        )

    async def list_task_instances(self) -> list[CollectorTaskInstance]:
        return list(self.instances)

    async def stop_task_instance(self, instance_id: str) -> CollectorTaskInstance:
        self.stopped.append(instance_id)
        for index, row in enumerate(self.instances):
            if row.instance_id == instance_id:
                updated = CollectorTaskInstance(
                    instance_id=row.instance_id,
                    task_id=row.task_id,
                    device_id=row.device_id,
                    point_group=row.point_group,
                    interval=row.interval,
                    targets=row.targets,
                    state="stopped",
                )
                self.instances[index] = updated
                return updated
        raise KeyError(instance_id)


class _Directory:
    def __init__(self, collectors: dict[str, _Collector]) -> None:
        self.collectors = collectors

    def get(self, worker_id: str) -> _Collector:
        return self.collectors[worker_id]

    def list_worker_ids(self) -> list[str]:
        return sorted(self.collectors)


def _instance(
    instance_id: str,
    task_id: str,
    *,
    state: str = "running",
) -> CollectorTaskInstance:
    return CollectorTaskInstance(
        instance_id=instance_id,
        task_id=task_id,
        device_id=instance_id,
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        state=state,
    )


def _setup(
    tmp_path,
    task_ids: list[str],
    worker_ids: list[str],
) -> tuple[_Config, dict[str, _Collector], TaskPlacementRegistry, TaskPlacementReconciler]:
    config = _Config(tmp_path, task_ids)
    collectors = {worker_id: _Collector(worker_id, []) for worker_id in worker_ids}
    directory = _Directory(collectors)
    placements = TaskPlacementRegistry(config, directory)
    reconciler = TaskPlacementReconciler(directory, placements, config)
    return config, collectors, placements, reconciler


@pytest.mark.asyncio
async def test_reconcile_stops_wrong_running_instance_and_opens_start_gate(
    tmp_path,
) -> None:
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    wrong_worker = next(worker for worker in collectors if worker != owner)
    collectors[wrong_worker].instances.append(_instance("d1", "task-a"))

    assert reconciler.placement_safe is False
    with pytest.raises(TaskPlacementUnsafeError):
        reconciler.require_safe_start()

    result = await reconciler.reconcile()

    assert result.safe is True
    assert result.generation == placements.generation
    assert result.scanned_workers == 2
    assert result.wrong_running_instances == 1
    assert result.stopped_instances == 1
    assert collectors[wrong_worker].stopped == ["d1"]
    assert reconciler.placement_safe is True


@pytest.mark.asyncio
async def test_reconcile_keeps_correct_running_instance(tmp_path) -> None:
    """跑在期望 Collector 上的实例不被停止。"""
    _, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("d1", "task-a"))

    result = await reconciler.reconcile()

    assert result.safe is True
    assert result.examined_instances == 1
    assert result.wrong_running_instances == 0
    assert result.stopped_instances == 0
    assert all(not collector.stopped for collector in collectors.values())


@pytest.mark.asyncio
async def test_reconcile_keeps_stopped_instance_on_wrong_worker(tmp_path) -> None:
    """错误 Worker 上的非 running 实例不属于 wrong-running，不做处理。"""
    _, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    wrong_worker = next(worker for worker in collectors if worker != owner)
    collectors[wrong_worker].instances.append(_instance("d1", "task-a", state="stopped"))

    result = await reconciler.reconcile()

    assert result.safe is True
    assert result.examined_instances == 1
    assert result.wrong_running_instances == 0
    assert collectors[wrong_worker].stopped == []


@pytest.mark.asyncio
async def test_reconcile_reports_unavailable_worker(tmp_path) -> None:
    """Collector 不可用时被报告且 safety 不成立。"""
    _, collectors, _, reconciler = _setup(tmp_path, ["task-a"], ["collector-a", "collector-b"])
    collectors["collector-b"].unavailable = True

    result = await reconciler.reconcile()

    assert result.safe is False
    assert result.unavailable_workers == ["collector-b"]
    assert any("collector-b" in error for error in result.errors)
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_reconcile_reports_identity_mismatch_as_unavailable(tmp_path) -> None:
    """Collector 上报身份与 placement worker_id 不一致时按不可用处理。"""
    _, collectors, _, reconciler = _setup(tmp_path, ["task-a"], ["collector-a", "collector-b"])
    collectors["collector-b"].reported_id = "someone-else"

    result = await reconciler.reconcile()

    assert result.safe is False
    assert result.unavailable_workers == ["collector-b"]
    assert any("identity mismatch" in error for error in result.errors)
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_reconcile_reports_orphaned_task(tmp_path) -> None:
    """ORPHANED placement 被报告且 safety 不成立。"""
    config = _Config(tmp_path, ["task-a"])
    collectors = {
        "collector-a": _Collector("collector-a", []),
        "collector-b": _Collector("collector-b", []),
    }
    placements = TaskPlacementRegistry(config, _Directory(collectors))
    owner = placements.worker_for_task("task-a")
    remaining = {
        worker_id: collector for worker_id, collector in collectors.items() if worker_id != owner
    }
    directory = _Directory(remaining)
    placements = TaskPlacementRegistry(config, directory)
    reconciler = TaskPlacementReconciler(directory, placements, config)

    result = await reconciler.reconcile()

    assert result.safe is False
    assert result.orphaned_tasks == ["task-a"]
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_placement_safe_only_for_current_generation(tmp_path) -> None:
    """placement 变化推进 generation 后，旧的收敛结果不再构成 safety。"""
    config, _, placements, reconciler = _setup(tmp_path, ["task-a"], ["collector-a", "collector-b"])

    result = await reconciler.reconcile()
    assert result.safe is True
    assert reconciler.placement_safe is True

    config.current_config.tasks["task-b"] = SimpleNamespace(
        task_id="task-b", device_group_id="d1", enabled=True
    )
    placements.sync()
    assert placements.generation > result.generation
    assert reconciler.placement_safe is False
    with pytest.raises(TaskPlacementUnsafeError):
        reconciler.require_safe_start()

    followup = await reconciler.reconcile()
    assert followup.safe is True
    assert reconciler.placement_safe is True


@pytest.mark.asyncio
async def test_invalidate_safety_closes_start_gate(tmp_path) -> None:
    """显式失效后 safety 立即关闭，需要重新收敛。"""
    _, _, _, reconciler = _setup(tmp_path, ["task-a"], ["collector-a", "collector-b"])
    await reconciler.reconcile()
    assert reconciler.placement_safe is True

    reconciler.invalidate_safety()

    assert reconciler.placement_safe is False
    with pytest.raises(TaskPlacementUnsafeError):
        reconciler.require_safe_start()


async def test_shared_device_across_workers_conflict_is_reported_without_stop(
    tmp_path, monkeypatch
) -> None:
    """同设备被两个启用 Task 分属不同 Worker：报告冲突、unsafe、不静默 STOP。"""
    tasks = {
        "task-a": SimpleNamespace(task_id="task-a", device_group_id="d1", enabled=True),
        "task-b": SimpleNamespace(task_id="task-b", device_group_id="d1", enabled=True),
    }
    config = _Config.__new__(_Config)
    config.config_dir = tmp_path
    config.current_config = SimpleNamespace(
        tasks=tasks,
        site=SimpleNamespace(
            devices={"d1": SimpleNamespace(device_id="d1", device_group_ids=("d1",), enabled=True)}
        ),
    )
    collectors = {
        worker_id: _Collector(worker_id, []) for worker_id in ("collector-a", "collector-b")
    }
    directory = _Directory(collectors)

    original = TaskPlacementRegistry._select_worker

    def forced(task_id: str, worker_ids: list[str]) -> str | None:
        if task_id == "task-a":
            return "collector-a"
        if task_id == "task-b":
            return "collector-b"
        return original(task_id, worker_ids)

    monkeypatch.setattr(TaskPlacementRegistry, "_select_worker", staticmethod(forced))
    placements = TaskPlacementRegistry(config, directory)
    reconciler = TaskPlacementReconciler(directory, placements, config)
    assert placements.worker_for_task("task-a") != placements.worker_for_task("task-b")

    # 冲突设备在两个 Worker 上都有 running 实例：任何一侧都不得被停止。
    collectors["collector-a"].instances.append(_instance("d1", "task-a"))
    collectors["collector-b"].instances.append(_instance("d1", "task-b"))
    result = await reconciler.reconcile()

    assert result.safe is False
    assert result.stopped_instances == 0
    assert result.wrong_running_instances == 0
    assert collectors["collector-a"].stopped == []
    assert collectors["collector-b"].stopped == []
    assert any("d1" in error and "conflict" in error for error in result.errors)
    assert reconciler.placement_safe is False

    # 冲突解除（task-b 禁用，只剩单一属主）后恢复安全收敛并清理错误 Worker。
    tasks["task-b"].enabled = False
    healed = await reconciler.reconcile()
    assert healed.safe is True
    assert healed.errors == []
    assert collectors["collector-a"].stopped == []
    assert collectors["collector-b"].stopped == ["d1"]


async def test_shared_device_on_same_worker_is_healthy(tmp_path) -> None:
    """同一设备多个启用 Task 归属同一 Worker：合法场景，safe 且不停止。"""
    tasks = {
        "task-a": SimpleNamespace(task_id="task-a", device_group_id="d1", enabled=True),
        "task-b": SimpleNamespace(task_id="task-b", device_group_id="d1", enabled=True),
    }
    config = _Config.__new__(_Config)
    config.config_dir = tmp_path
    config.current_config = SimpleNamespace(
        tasks=tasks,
        site=SimpleNamespace(
            devices={"d1": SimpleNamespace(device_id="d1", device_group_ids=("d1",), enabled=True)}
        ),
    )
    collectors = {"collector-a": _Collector("collector-a", [])}
    directory = _Directory(collectors)
    placements = TaskPlacementRegistry(config, directory)
    assert placements.worker_for_task("task-a") == "collector-a"
    assert placements.worker_for_task("task-b") == "collector-a"
    reconciler = TaskPlacementReconciler(directory, placements, config)
    collectors["collector-a"].instances.append(_instance("d1", "task-a"))

    result = await reconciler.reconcile()

    assert result.safe is True
    assert result.errors == []
    assert result.stopped_instances == 0
    assert collectors["collector-a"].stopped == []
