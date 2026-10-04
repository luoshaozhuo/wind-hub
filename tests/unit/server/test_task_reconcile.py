"""TaskPlacementReconciler 收敛与 placement safety 单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wind_hub_server.application.task.placement import TaskPlacementRegistry
from wind_hub_server.application.task.reconcile import (
    TaskPlacementReconciler,
    TaskPlacementUnsafeError,
)


class _Config:
    def __init__(self, config_dir, task_ids: list[str]) -> None:
        self.config_dir = config_dir
        self.current_config = SimpleNamespace(
            tasks=SimpleNamespace(
                tasks=[
                    SimpleNamespace(task_id=task_id, enabled=True)
                    for task_id in task_ids
                ]
            )
        )


class _Collector:
    def __init__(self, worker_id: str, instances: list[dict[str, object]]) -> None:
        self.worker_id = worker_id
        self.instances = [dict(row) for row in instances]
        self.stopped: list[str] = []
        self.applied_generation = 0
        self.assigned_task_ids: list[str] = []
        self.reported_id = worker_id
        self.generation_ack: int | None = None
        self.task_count_ack: int | None = None
        self.unavailable = False

    async def config_status(self) -> dict[str, object]:
        if self.unavailable:
            raise ConnectionError("unreachable")
        return {"collector_id": self.reported_id}

    async def apply_task_placement(
        self,
        worker_id: str,
        generation: int,
        task_ids: list[str],
    ) -> dict[str, object]:
        assert worker_id == self.worker_id
        self.applied_generation = generation
        self.assigned_task_ids = list(task_ids)
        return {
            "success": True,
            "generation": (
                generation if self.generation_ack is None else self.generation_ack
            ),
            "task_count": (
                len(task_ids) if self.task_count_ack is None else self.task_count_ack
            ),
        }

    async def list_task_instances(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.instances]

    async def stop_task_instance(self, instance_id: str) -> dict[str, object]:
        self.stopped.append(instance_id)
        for row in self.instances:
            if row.get("instance_id") == instance_id:
                row["state"] = "stopped"
                return dict(row)
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
) -> dict[str, object]:
    return {
        "instance_id": instance_id,
        "task_id": task_id,
        "device_id": "d1",
        "point_group": "fast",
        "interval": 1.0,
        "targets": ["archive"],
        "state": state,
    }


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
    collectors[wrong_worker].instances.append(_instance("task-a:d1", "task-a"))

    assert reconciler.placement_safe is False
    with pytest.raises(TaskPlacementUnsafeError):
        reconciler.require_safe_start()

    result = await reconciler.reconcile()

    assert result.safe is True
    assert result.generation == placements.generation
    assert result.scanned_workers == 2
    assert result.wrong_running_instances == 1
    assert result.stopped_instances == 1
    assert collectors[wrong_worker].stopped == ["task-a:d1"]
    assert collectors[owner].applied_generation == placements.generation
    assert collectors[owner].assigned_task_ids == ["task-a"]
    assert reconciler.placement_safe is True


@pytest.mark.asyncio
async def test_reconcile_keeps_correct_running_instance(tmp_path) -> None:
    """跑在期望 Collector 上的实例不被停止。"""
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("task-a:d1", "task-a"))

    result = await reconciler.reconcile()

    assert result.safe is True
    assert result.examined_instances == 1
    assert result.wrong_running_instances == 0
    assert result.stopped_instances == 0
    assert all(not collector.stopped for collector in collectors.values())


@pytest.mark.asyncio
async def test_reconcile_keeps_stopped_instance_on_wrong_worker(tmp_path) -> None:
    """错误 Worker 上的非 running 实例不属于 wrong-running，不做处理。"""
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    wrong_worker = next(worker for worker in collectors if worker != owner)
    collectors[wrong_worker].instances.append(
        _instance("task-a:d1", "task-a", state="stopped")
    )

    result = await reconciler.reconcile()

    assert result.safe is True
    assert result.examined_instances == 1
    assert result.wrong_running_instances == 0
    assert collectors[wrong_worker].stopped == []


@pytest.mark.asyncio
async def test_reconcile_reports_unavailable_worker(tmp_path) -> None:
    """Collector 不可用时被报告且 safety 不成立。"""
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    collectors["collector-b"].unavailable = True

    result = await reconciler.reconcile()

    assert result.safe is False
    assert result.unavailable_workers == ["collector-b"]
    assert any("collector-b" in error for error in result.errors)
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_reconcile_reports_identity_mismatch_as_unavailable(tmp_path) -> None:
    """Collector 上报身份与 placement worker_id 不一致时按不可用处理。"""
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
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
        worker_id: collector
        for worker_id, collector in collectors.items()
        if worker_id != owner
    }
    directory = _Directory(remaining)
    placements = TaskPlacementRegistry(config, directory)
    reconciler = TaskPlacementReconciler(directory, placements, config)

    result = await reconciler.reconcile()

    assert result.safe is False
    assert result.orphaned_tasks == ["task-a"]
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_reconcile_rejects_generation_ack_mismatch(tmp_path) -> None:
    """Collector placement generation ack 不一致时收敛失败。"""
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    collectors[owner].generation_ack = placements.generation + 1

    result = await reconciler.reconcile()

    assert result.safe is False
    assert owner in result.unavailable_workers
    assert any("generation acknowledgment mismatch" in error for error in result.errors)
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_reconcile_rejects_task_count_ack_mismatch(tmp_path) -> None:
    """Collector placement task_count ack 不一致时收敛失败。"""
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    collectors[owner].task_count_ack = 99

    result = await reconciler.reconcile()

    assert result.safe is False
    assert owner in result.unavailable_workers
    assert any("task-count acknowledgment mismatch" in error for error in result.errors)
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_placement_safe_only_for_current_generation(tmp_path) -> None:
    """placement 变化推进 generation 后，旧的收敛结果不再构成 safety。"""
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )

    result = await reconciler.reconcile()
    assert result.safe is True
    assert reconciler.placement_safe is True

    config.current_config.tasks.tasks.append(
        SimpleNamespace(task_id="task-b", enabled=True)
    )
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
    config, collectors, placements, reconciler = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    await reconciler.reconcile()
    assert reconciler.placement_safe is True

    reconciler.invalidate_safety()

    assert reconciler.placement_safe is False
    with pytest.raises(TaskPlacementUnsafeError):
        reconciler.require_safe_start()
