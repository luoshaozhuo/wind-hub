"""TaskControlService start/stop 与运行状态汇总单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wind_hub_server.application.port.monitoring import TaskRuntimeSnapshot
from wind_hub_server.application.port.worker import (
    CollectorInfo,
    CollectorPlacementRejectedError,
    CollectorTaskInstance,
    CollectorTaskSummary,
    PlacementAck,
)
from wind_hub_server.application.task.collector import TaskWorkerUnavailableError
from wind_hub_server.application.task.control import TaskControlService
from wind_hub_server.application.task.placement import (
    TaskPlacementRegistry,
    TaskPlacementState,
)
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
                    SimpleNamespace(
                        task_id=task_id,
                        device="d1",
                        device_group=None,
                        point_group="fast",
                        interval=1.0,
                        targets=[SimpleNamespace(sink="archive")],
                        enabled=True,
                    )
                    for task_id in task_ids
                ]
            )
        )


class _Monitoring:
    def __init__(self, runtime_rows: list[TaskRuntimeSnapshot] | None = None) -> None:
        self.runtime_rows = runtime_rows or []
        self.refreshes = 0

    def tasks_snapshot(self) -> list[TaskRuntimeSnapshot]:
        return list(self.runtime_rows)

    async def refresh_now(self) -> None:
        self.refreshes += 1


def _collector_info(reported_id: str) -> CollectorInfo:
    return CollectorInfo(
        component="collector",
        collector_id=reported_id,
        boot_id="boot-1",
        config_hash="hash",
        active_config_hash="hash",
        prepared_config_hash=None,
        boot_config_hash="hash",
        config_revision=None,
        active_revision="",
        prepared_revision=None,
        runtime_running=True,
    )


def _task_summary(task_id: str, runtime_state: str) -> CollectorTaskSummary:
    running = runtime_state == "running"
    return CollectorTaskSummary(
        task_id=task_id,
        device="d1",
        device_group=None,
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        enabled=True,
        runtime_state=runtime_state,
        instance_count=1,
        running_instances=1 if running else 0,
        stopped_instances=0 if running else 1,
        failed_instances=0,
    )


class _Collector:
    def __init__(self, worker_id: str, instances: list[CollectorTaskInstance]) -> None:
        self.worker_id = worker_id
        self.instances = list(instances)
        self.reported_id = worker_id
        self.unavailable = False
        self.reject_placement_start = False
        self.started_tasks: list[tuple[str, int]] = []
        self.stopped_tasks: list[str] = []
        self.started_instances: list[tuple[str, int]] = []
        self.stopped_instances: list[str] = []

    async def config_status(self) -> CollectorInfo:
        if self.unavailable:
            raise ConnectionError("unreachable")
        return _collector_info(self.reported_id)

    async def apply_task_placement(
        self,
        worker_id: str,
        generation: int,
        task_ids: list[str],
    ) -> PlacementAck:
        assert worker_id == self.worker_id
        return PlacementAck(
            success=True,
            generation=generation,
            task_count=len(task_ids),
        )

    async def list_task_instances(self) -> list[CollectorTaskInstance]:
        return list(self.instances)

    async def start_task(
        self,
        task_id: str,
        placement_generation: int,
    ) -> CollectorTaskSummary:
        if self.reject_placement_start:
            raise CollectorPlacementRejectedError("stale placement generation")
        self.started_tasks.append((task_id, placement_generation))
        return _task_summary(task_id, "running")

    async def stop_task(self, task_id: str) -> CollectorTaskSummary:
        self.stopped_tasks.append(task_id)
        return _task_summary(task_id, "stopped")

    def _replace_instance(
        self,
        instance_id: str,
        state: str,
    ) -> CollectorTaskInstance:
        for index, row in enumerate(self.instances):
            if row.instance_id == instance_id:
                updated = CollectorTaskInstance(
                    instance_id=row.instance_id,
                    task_id=row.task_id,
                    device_id=row.device_id,
                    point_group=row.point_group,
                    interval=row.interval,
                    targets=row.targets,
                    state=state,
                )
                self.instances[index] = updated
                return updated
        raise KeyError(instance_id)

    async def start_task_instance(
        self,
        instance_id: str,
        placement_generation: int,
    ) -> CollectorTaskInstance:
        self.started_instances.append((instance_id, placement_generation))
        return self._replace_instance(instance_id, "running")

    async def stop_task_instance(self, instance_id: str) -> CollectorTaskInstance:
        self.stopped_instances.append(instance_id)
        return self._replace_instance(instance_id, "stopped")


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
    state: str = "stopped",
) -> CollectorTaskInstance:
    return CollectorTaskInstance(
        instance_id=instance_id,
        task_id=task_id,
        device_id="d1",
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        state=state,
    )


def _setup(
    tmp_path,
    task_ids: list[str],
    worker_ids: list[str],
    *,
    monitoring: _Monitoring | None = None,
) -> tuple[
    dict[str, _Collector],
    TaskPlacementRegistry,
    TaskPlacementReconciler,
    TaskControlService,
    _Monitoring,
]:
    config = _Config(tmp_path, task_ids)
    collectors = {worker_id: _Collector(worker_id, []) for worker_id in worker_ids}
    directory = _Directory(collectors)
    placements = TaskPlacementRegistry(config, directory)
    reconciler = TaskPlacementReconciler(directory, placements, config)
    monitor = monitoring or _Monitoring()
    control = TaskControlService(directory, placements, reconciler, config, monitor)
    return collectors, placements, reconciler, control, monitor


@pytest.mark.asyncio
async def test_start_task_blocked_until_placement_safe(tmp_path) -> None:
    """未完成安全收敛时 start 被拒绝。"""
    _, _, _, control, _ = _setup(tmp_path, ["task-a"], ["collector-a"])

    with pytest.raises(TaskPlacementUnsafeError):
        await control.start_task("task-a")


@pytest.mark.asyncio
async def test_start_instance_blocked_until_placement_safe(tmp_path) -> None:
    collectors, _, _, control, _ = _setup(tmp_path, ["task-a"], ["collector-a"])
    collectors["collector-a"].instances.append(_instance("task-a:d1", "task-a"))

    with pytest.raises(TaskPlacementUnsafeError):
        await control.start_instance("task-a:d1")


@pytest.mark.asyncio
async def test_start_task_allowed_after_reconcile(tmp_path) -> None:
    """安全收敛后 start 携带当前 generation 下发并刷新监控。"""
    collectors, placements, reconciler, control, monitor = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    await reconciler.reconcile()
    owner = placements.worker_for_task("task-a")

    summary = await control.start_task("task-a")

    assert summary.runtime_state == "running"
    assert summary.assigned_worker_id == owner
    assert summary.placement_state is TaskPlacementState.ASSIGNED
    assert collectors[owner].started_tasks == [("task-a", placements.generation)]
    assert monitor.refreshes == 1


@pytest.mark.asyncio
async def test_start_task_identity_mismatch(tmp_path) -> None:
    """Collector 身份不一致时 start 报 Worker 不可用。"""
    collectors, _, reconciler, control, _ = _setup(tmp_path, ["task-a"], ["collector-a"])
    await reconciler.reconcile()
    collectors["collector-a"].reported_id = "someone-else"

    with pytest.raises(TaskWorkerUnavailableError, match="identity mismatch"):
        await control.start_task("task-a")


@pytest.mark.asyncio
async def test_start_task_collector_unavailable(tmp_path) -> None:
    """Collector 不可达时 start 报 Worker 不可用。"""
    collectors, _, reconciler, control, _ = _setup(tmp_path, ["task-a"], ["collector-a"])
    await reconciler.reconcile()
    collectors["collector-a"].unavailable = True

    with pytest.raises(TaskWorkerUnavailableError, match="unavailable"):
        await control.start_task("task-a")


@pytest.mark.asyncio
async def test_start_task_rejected_closes_safety_gate(tmp_path) -> None:
    """Collector 拒绝受 placement 保护的 start 后安全栅栏关闭。"""
    collectors, _, reconciler, control, _ = _setup(tmp_path, ["task-a"], ["collector-a"])
    await reconciler.reconcile()
    collectors["collector-a"].reject_placement_start = True

    with pytest.raises(TaskPlacementUnsafeError, match="stale placement generation"):
        await control.start_task("task-a")
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_stop_task_does_not_require_safe_placement(tmp_path) -> None:
    """stop 不受 placement safety 栅栏限制。"""
    collectors, _, _, control, monitor = _setup(tmp_path, ["task-a"], ["collector-a"])

    summary = await control.stop_task("task-a")

    assert summary.runtime_state == "stopped"
    assert collectors["collector-a"].stopped_tasks == ["task-a"]
    assert monitor.refreshes == 1


@pytest.mark.asyncio
async def test_start_and_stop_instance_after_reconcile(tmp_path) -> None:
    collectors, placements, reconciler, control, monitor = _setup(
        tmp_path, ["task-a"], ["collector-a"]
    )
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("task-a:d1", "task-a"))
    await reconciler.reconcile()

    started = await control.start_instance("task-a:d1")
    assert started.state.value == "running"
    assert started.assigned_worker_id == owner
    assert collectors[owner].started_instances == [
        ("task-a:d1", placements.generation)
    ]

    stopped = await control.stop_instance("task-a:d1")
    assert stopped.state.value == "stopped"
    assert collectors[owner].stopped_instances == ["task-a:d1"]
    assert monitor.refreshes == 2


def test_task_summary_merges_monitoring_runtime(tmp_path) -> None:
    """配置字段以 Server 基线为权威，运行态来自 Monitoring 快照。"""
    monitoring = _Monitoring(
        [
            TaskRuntimeSnapshot(
                task_id="task-a",
                device="stale-device",
                device_group=None,
                point_group="stale",
                interval=9.0,
                targets=["stale"],
                enabled=False,
                runtime_state="running",
                instance_count=2,
                running_instances=1,
                stopped_instances=0,
                failed_instances=1,
                assigned_worker_id="collector-a",
            )
        ]
    )
    _, placements, _, control, _ = _setup(
        tmp_path, ["task-a"], ["collector-a"], monitoring=monitoring
    )

    summary = control.get_task_summary("task-a")

    assert summary.device == "d1"
    assert summary.point_group == "fast"
    assert summary.enabled is True
    assert summary.targets == ["archive"]
    assert summary.runtime_state == "running"
    assert summary.instance_count == 2
    assert summary.failed_instances == 1
    assert summary.assigned_worker_id == placements.worker_for_task("task-a")
    assert summary.placement_state is TaskPlacementState.ASSIGNED


def test_task_summary_fallback_for_unassigned(tmp_path) -> None:
    """无可用 Collector 时汇总状态诚实标记为 unassigned。"""
    _, _, _, control, _ = _setup(tmp_path, ["task-a"], [])

    summary = control.get_task_summary("task-a")

    assert summary.assigned_worker_id is None
    assert summary.placement_state is TaskPlacementState.UNASSIGNED
    assert summary.runtime_state == "unassigned"
    assert summary.instance_count == 0


@pytest.mark.asyncio
async def test_list_instances_only_returns_assigned_worker_rows(tmp_path) -> None:
    """实例查询只返回承载该 Task 的 Collector 行，并标注 assigned_worker_id。"""
    collectors, placements, reconciler, control, _ = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"]
    )
    owner = placements.worker_for_task("task-a")
    wrong = next(worker for worker in collectors if worker != owner)
    collectors[owner].instances.append(_instance("task-a:d1", "task-a"))
    collectors[wrong].instances.append(_instance("foreign:d9", "task-foreign"))
    await reconciler.reconcile()

    rows = await control.list_task_instances("task-a")

    assert [row.instance_id for row in rows] == ["task-a:d1"]
    assert rows[0].assigned_worker_id == owner
