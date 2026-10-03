"""Task placement 持久化与收敛单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wind_hub_server.application.usecase.task_assignment import (
    TaskAssignmentUseCase,
    TaskPlacementError,
    TaskPlacementState,
)
from wind_hub_server.application.usecase.worker_tasks import (
    CollectorTaskUseCase,
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

    async def config_status(self) -> dict[str, object]:
        return {"collector_id": self.worker_id}

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
            "generation": generation,
            "task_count": len(task_ids),
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


def test_removed_owner_stays_orphaned_after_restart(tmp_path) -> None:
    config = _Config(tmp_path, ["task-a"])
    first_directory = _Directory(
        {
            "collector-a": _Collector("collector-a", []),
            "collector-b": _Collector("collector-b", []),
        }
    )
    first = TaskAssignmentUseCase(config, first_directory)
    original = first.assignment_for_task("task-a")
    original_generation = first.generation
    assert original.worker_id is not None

    remaining = {
        worker_id: collector
        for worker_id, collector in first_directory.collectors.items()
        if worker_id != original.worker_id
    }
    restarted = TaskAssignmentUseCase(config, _Directory(remaining))
    restored = restarted.assignment_for_task("task-a")

    assert restored.worker_id == original.worker_id
    assert restored.state is TaskPlacementState.ORPHANED
    assert restarted.generation > original_generation
    with pytest.raises(TaskPlacementError, match="orphaned"):
        restarted.worker_for_task("task-a")


@pytest.mark.asyncio
async def test_reconcile_stops_wrong_running_instance_and_opens_start_gate(
    tmp_path,
) -> None:
    config = _Config(tmp_path, ["task-a"])
    collectors = {
        "collector-a": _Collector("collector-a", []),
        "collector-b": _Collector("collector-b", []),
    }
    directory = _Directory(collectors)
    assignments = TaskAssignmentUseCase(config, directory)
    owner = assignments.worker_for_task("task-a")
    wrong_worker = next(worker for worker in collectors if worker != owner)
    collectors[wrong_worker].instances.append(
        {
            "instance_id": "task-a:d1",
            "task_id": "task-a",
            "device_id": "d1",
            "point_group": "fast",
            "interval": 1.0,
            "targets": ["archive"],
            "state": "running",
        }
    )

    monitoring = SimpleNamespace(tasks_snapshot=lambda: [])
    tasks = CollectorTaskUseCase(directory, assignments, config, monitoring)
    assert tasks.placement_safe is False
    with pytest.raises(TaskPlacementUnsafeError):
        tasks._require_safe_start()

    result = await tasks.reconcile_placement()

    assert result.safe is True
    assert result.wrong_running_instances == 1
    assert result.stopped_instances == 1
    assert collectors[wrong_worker].stopped == ["task-a:d1"]
    assert collectors[owner].applied_generation == assignments.generation
    assert collectors[owner].assigned_task_ids == ["task-a"]
    assert tasks.placement_safe is True
