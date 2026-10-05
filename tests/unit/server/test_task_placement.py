"""TaskPlacementRegistry 持久化与 placement 状态单元测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from wind_hub_server.application.task.placement import (
    TaskPlacementError,
    TaskPlacementRegistry,
    TaskPlacementState,
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


class _Directory:
    def __init__(self, worker_ids: list[str]) -> None:
        self.worker_ids = sorted(worker_ids)

    def get(self, worker_id: str) -> object:
        if worker_id not in self.worker_ids:
            raise KeyError(worker_id)
        return object()

    def list_worker_ids(self) -> list[str]:
        return list(self.worker_ids)


def test_rendezvous_placement_is_stable(tmp_path) -> None:
    """rendezvous hashing 结果不随重构变化。"""
    config = _Config(tmp_path, ["task-a", "task-b", "task-c", "task-d"])
    directory = _Directory(["collector-a", "collector-b"])
    registry = TaskPlacementRegistry(config, directory)

    placements = {
        row.task_id: row.worker_id for row in registry.list_placements()
    }
    assert placements == {
        "task-a": "collector-a",
        "task-b": "collector-a",
        "task-c": "collector-b",
        "task-d": "collector-b",
    }


def test_persist_load_roundtrip(tmp_path) -> None:
    """状态文件重载后 placement 与 generation 完全一致。"""
    config = _Config(tmp_path, ["task-a", "task-c"])
    directory = _Directory(["collector-a", "collector-b"])
    first = TaskPlacementRegistry(config, directory)
    expected = {
        row.task_id: (row.worker_id, row.state) for row in first.list_placements()
    }

    reloaded = TaskPlacementRegistry(config, directory)

    assert reloaded.generation == first.generation
    assert {
        row.task_id: (row.worker_id, row.state) for row in reloaded.list_placements()
    } == expected


def test_legacy_state_file_loads(tmp_path) -> None:
    """既有 v2 状态文件（重构前写入格式）仍可加载。"""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    (state_dir / "task-placement.json").write_text(
        json.dumps(
            {
                "version": 2,
                "generation": 7,
                "workers": ["collector-a", "collector-b"],
                "placements": {"task-a": "collector-b"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    config = _Config(tmp_path, ["task-a"])
    directory = _Directory(["collector-a", "collector-b"])

    registry = TaskPlacementRegistry(config, directory)

    placement = registry.placement_for_task("task-a")
    assert placement.worker_id == "collector-b"
    assert placement.state is TaskPlacementState.ASSIGNED
    assert registry.generation == 7
    assert registry.worker_for_task("task-a") == "collector-b"


def test_worker_set_change_keeps_assigned_and_orphaned(tmp_path) -> None:
    """Worker 集合变化不迁移已有 Owner：ASSIGNED 不变，移除的 Owner 变 ORPHANED。"""
    config = _Config(tmp_path, ["task-a", "task-c"])
    first = TaskPlacementRegistry(
        config, _Directory(["collector-a", "collector-b"])
    )
    original = {
        row.task_id: row.worker_id for row in first.list_placements()
    }
    assert original["task-a"] == "collector-a"
    assert original["task-c"] == "collector-b"
    original_generation = first.generation

    restarted = TaskPlacementRegistry(config, _Directory(["collector-a"]))
    placements = {
        row.task_id: row for row in restarted.list_placements()
    }

    assert placements["task-a"].worker_id == "collector-a"
    assert placements["task-a"].state is TaskPlacementState.ASSIGNED
    assert placements["task-c"].worker_id == "collector-b"
    assert placements["task-c"].state is TaskPlacementState.ORPHANED
    assert restarted.generation > original_generation
    with pytest.raises(TaskPlacementError, match="orphaned"):
        restarted.worker_for_task("task-c")


def test_removed_owner_stays_orphaned_after_restart(tmp_path) -> None:
    config = _Config(tmp_path, ["task-a"])
    first = TaskPlacementRegistry(
        config, _Directory(["collector-a", "collector-b"])
    )
    original = first.placement_for_task("task-a")
    original_generation = first.generation
    assert original.worker_id is not None

    remaining = [
        worker_id
        for worker_id in ("collector-a", "collector-b")
        if worker_id != original.worker_id
    ]
    restarted = TaskPlacementRegistry(config, _Directory(remaining))
    restored = restarted.placement_for_task("task-a")

    assert restored.worker_id == original.worker_id
    assert restored.state is TaskPlacementState.ORPHANED
    assert restarted.generation > original_generation
    with pytest.raises(TaskPlacementError, match="orphaned"):
        restarted.worker_for_task("task-a")


def test_generation_stable_without_topology_change(tmp_path) -> None:
    """Worker 集合与 Task 集合不变时 generation 不增长。"""
    config = _Config(tmp_path, ["task-a"])
    directory = _Directory(["collector-a", "collector-b"])
    registry = TaskPlacementRegistry(config, directory)
    assert registry.generation == 1

    registry.sync()
    registry.list_placements()
    registry.placement_for_task("task-a")

    assert registry.generation == 1


def test_unassigned_task_has_no_worker(tmp_path) -> None:
    """没有可用 Collector 时 placement 为 UNASSIGNED。"""
    config = _Config(tmp_path, ["task-a"])
    registry = TaskPlacementRegistry(config, _Directory([]))

    placement = registry.placement_for_task("task-a")
    assert placement.worker_id is None
    assert placement.state is TaskPlacementState.UNASSIGNED
    with pytest.raises(TaskPlacementError, match="unassigned"):
        registry.worker_for_task("task-a")
