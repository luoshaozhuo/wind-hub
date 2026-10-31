"""TaskControlService start/stop 与运行状态汇总单元测试（Collector v2 语义）。

Collector 侧 placement 栅栏已移除（设备级 START/STOP + revision），但 Server
仍维护自己的 Task→Worker placement 注册表与安全收敛栅栏；本文件只测 Server
这一层：未 reconcile 禁止 start、stop 不受栅栏限制、冲突使栅栏失效、
汇总合并 Monitoring 快照、实例只返回归属 Worker 的行。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from web_server.application.port.monitoring import TaskRuntimeSnapshot
from web_server.application.port.worker import (
    CollectorInfo,
    CollectorTaskConflictError,
    CollectorTaskInstance,
    CollectorTaskSummary,
)
from web_server.application.task.collector import TaskWorkerUnavailableError
from web_server.application.task.control import TaskControlService
from web_server.application.task.placement import (
    TaskPlacementRegistry,
    TaskPlacementState,
)
from web_server.application.task.reconcile import (
    TaskPlacementReconciler,
    TaskPlacementUnsafeError,
)

pytestmark = pytest.mark.unit


class _Config:
    def __init__(
        self,
        config_dir,
        task_ids: list[str],
        *,
        devices: dict[str, SimpleNamespace] | None = None,
        tasks: dict[str, SimpleNamespace] | None = None,
    ) -> None:
        self.config_dir = config_dir
        if devices is None:
            devices = {
                "d1": SimpleNamespace(device_id="d1", device_group_ids=("d1",), enabled=True)
            }
        self.current_config = SimpleNamespace(
            tasks=tasks
            or {
                task_id: SimpleNamespace(
                    task_id=task_id,
                    device_group_id="d1",
                    point_group="fast",
                    interval=1.0,
                    enabled=True,
                )
                for task_id in task_ids
            },
            # Sink 绑定落在 sinks.<id>.outputs（以 task_id 为键）。
            sinks={"archive": SimpleNamespace(outputs={task_id: {} for task_id in task_ids})},
            # v2：Task Definition 经 device_group_id 展开到设备集合。
            site=SimpleNamespace(devices=devices),
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
        collector_id=reported_id,
        boot_id="boot-1",
        active_revision="rev-1",
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
    """v2 Worker 替身：设备粒度 start/stop，revision 冲突以异常上抛。"""

    def __init__(self, worker_id: str, instances: list[CollectorTaskInstance]) -> None:
        self.worker_id = worker_id
        self.instances = list(instances)
        self.reported_id = worker_id
        self.unavailable = False
        self.conflict_on_start = False
        self.conflict_devices: set[str] = set()
        self.generic_fail_devices: set[str] = set()
        self.started_tasks: list[str] = []
        self.stopped_tasks: list[str] = []
        self.started_instances: list[str] = []
        self.stopped_instances: list[str] = []

    async def config_status(self) -> CollectorInfo:
        if self.unavailable:
            raise ConnectionError("unreachable")
        return _collector_info(self.reported_id)

    async def list_task_instances(self) -> list[CollectorTaskInstance]:
        return list(self.instances)

    async def start_task(self, device_id: str) -> CollectorTaskSummary:
        if self.conflict_on_start or device_id in self.conflict_devices:
            raise CollectorTaskConflictError("configuration revision conflict")
        if device_id in self.generic_fail_devices:
            raise RuntimeError(f"device {device_id} start failed")
        self.started_tasks.append(device_id)
        return _task_summary(device_id, "running")

    async def stop_task(self, device_id: str) -> CollectorTaskSummary:
        self.stopped_tasks.append(device_id)
        return _task_summary(device_id, "stopped")

    def _replace_instance(self, device_id: str, state: str) -> CollectorTaskInstance:
        for index, row in enumerate(self.instances):
            if row.instance_id == device_id:
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
        raise KeyError(device_id)

    async def start_task_instance(self, device_id: str) -> CollectorTaskInstance:
        if self.conflict_on_start:
            raise CollectorTaskConflictError("configuration revision conflict")
        self.started_instances.append(device_id)
        return self._replace_instance(device_id, "running")

    async def stop_task_instance(self, device_id: str) -> CollectorTaskInstance:
        self.stopped_instances.append(device_id)
        return self._replace_instance(device_id, "stopped")


class _Directory:
    def __init__(self, collectors: dict[str, _Collector]) -> None:
        self.collectors = collectors

    def get(self, worker_id: str) -> _Collector:
        return self.collectors[worker_id]

    def list_worker_ids(self) -> list[str]:
        return sorted(self.collectors)


def _instance(device_id: str, task_id: str, *, state: str = "stopped") -> CollectorTaskInstance:
    return CollectorTaskInstance(
        instance_id=device_id,
        task_id=task_id,
        device_id=device_id,
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
    collectors["collector-a"].instances.append(_instance("d1", "task-a"))

    with pytest.raises(TaskPlacementUnsafeError):
        await control.start_instance("d1")


@pytest.mark.asyncio
async def test_start_task_allowed_after_reconcile(tmp_path) -> None:
    """安全收敛后 start 以设备粒度下发并刷新监控。"""
    monitoring = _Monitoring(
        [
            TaskRuntimeSnapshot(
                task_id="task-a",
                device="d1",
                device_group="d1",
                point_group="fast",
                interval=1.0,
                targets=["archive"],
                enabled=True,
                runtime_state="running",
                instance_count=1,
                running_instances=1,
                stopped_instances=0,
                failed_instances=0,
                assigned_worker_id=None,
            )
        ]
    )
    collectors, placements, reconciler, control, monitor = _setup(
        tmp_path, ["task-a"], ["collector-a", "collector-b"], monitoring=monitoring
    )
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("d1", "task-a"))
    await reconciler.reconcile()

    summary = await control.start_task("task-a")

    assert summary.runtime_state == "running"
    assert summary.assigned_worker_id == owner
    assert summary.placement_state is TaskPlacementState.ASSIGNED
    # v2 扇出以 device_id 为身份下发，不再传 placement generation。
    assert collectors[owner].started_tasks == ["d1"]
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
async def test_start_task_conflict_closes_safety_gate(tmp_path) -> None:
    """Collector 以 revision 冲突拒绝 START 后安全栅栏关闭。"""
    collectors, _, reconciler, control, _ = _setup(tmp_path, ["task-a"], ["collector-a"])
    collectors["collector-a"].instances.append(_instance("d1", "task-a"))
    await reconciler.reconcile()
    collectors["collector-a"].conflict_on_start = True

    with pytest.raises(TaskPlacementUnsafeError, match="revision conflict"):
        await control.start_task("task-a")
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_stop_task_does_not_require_safe_placement(tmp_path) -> None:
    """stop 不受 placement safety 栅栏限制。"""
    monitoring = _Monitoring(
        [
            TaskRuntimeSnapshot(
                task_id="task-a",
                device="d1",
                device_group="d1",
                point_group="fast",
                interval=1.0,
                targets=["archive"],
                enabled=True,
                runtime_state="stopped",
                instance_count=1,
                running_instances=0,
                stopped_instances=1,
                failed_instances=0,
                assigned_worker_id=None,
            )
        ]
    )
    collectors, _, _, control, monitor = _setup(
        tmp_path, ["task-a"], ["collector-a"], monitoring=monitoring
    )
    collectors["collector-a"].instances.append(_instance("d1", "task-a", state="running"))

    summary = await control.stop_task("task-a")

    assert summary.runtime_state == "stopped"
    assert collectors["collector-a"].stopped_tasks == ["d1"]
    assert monitor.refreshes == 1


@pytest.mark.asyncio
async def test_start_and_stop_instance_after_reconcile(tmp_path) -> None:
    collectors, placements, reconciler, control, monitor = _setup(
        tmp_path, ["task-a"], ["collector-a"]
    )
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("d1", "task-a"))
    await reconciler.reconcile()

    started = await control.start_instance("d1")
    assert started.state.value == "running"
    assert started.assigned_worker_id == owner
    assert collectors[owner].started_instances == ["d1"]

    stopped = await control.stop_instance("d1")
    assert stopped.state.value == "stopped"
    assert collectors[owner].stopped_instances == ["d1"]
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

    assert summary.device_group == "d1"
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
    collectors[owner].instances.append(_instance("d1", "task-a"))
    collectors[wrong].instances.append(_instance("d9", "task-foreign"))
    await reconciler.reconcile()

    rows = await control.list_task_instances("task-a")

    assert [row.instance_id for row in rows] == ["d1"]
    assert rows[0].assigned_worker_id == owner


def _group_device(device_id: str, group: str = "d1", *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(device_id=device_id, device_group_ids=(group,), enabled=enabled)


def _assemble_with(
    tmp_path,
    config: _Config,
    worker_ids: list[str],
) -> tuple[
    dict[str, _Collector], TaskPlacementRegistry, TaskPlacementReconciler, TaskControlService
]:
    collectors = {worker_id: _Collector(worker_id, []) for worker_id in worker_ids}
    directory = _Directory(collectors)
    placements = TaskPlacementRegistry(config, directory)
    reconciler = TaskPlacementReconciler(directory, placements, config)
    control = TaskControlService(directory, placements, reconciler, config, _Monitoring())
    return collectors, placements, reconciler, control


@pytest.mark.asyncio
async def test_start_task_fans_out_to_every_enabled_device(tmp_path) -> None:
    """多设备任务组：start 按 device_id 扇出到每台启用设备，无遗漏无多余。"""
    devices = {f"d{i}": _group_device(f"d{i}") for i in (1, 2, 3)}
    config = _Config(tmp_path, ["task-a"], devices=devices)
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.extend(_instance(f"d{i}", "task-a") for i in (1, 2, 3))
    await reconciler.reconcile()

    await control.start_task("task-a")

    assert sorted(collectors[owner].started_tasks) == ["d1", "d2", "d3"]


@pytest.mark.asyncio
async def test_start_task_skips_disabled_devices(tmp_path) -> None:
    """组内被禁用的设备不参与扇出（禁用设备不产生采集实例）。"""
    devices = {
        "d1": _group_device("d1"),
        "d2": _group_device("d2", enabled=False),
    }
    config = _Config(tmp_path, ["task-a"], devices=devices)
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("d1", "task-a"))
    await reconciler.reconcile()

    await control.start_task("task-a")

    assert collectors[owner].started_tasks == ["d1"]


@pytest.mark.asyncio
async def test_start_task_conflict_mid_fanout_closes_gate_and_stops_dispatch(
    tmp_path,
) -> None:
    """第二台设备 revision 冲突：栅栏关闭，后续设备不再下发，异常上抛不报成功。"""
    devices = {f"d{i}": _group_device(f"d{i}") for i in (1, 2, 3)}
    config = _Config(tmp_path, ["task-a"], devices=devices)
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.extend(_instance(f"d{i}", "task-a") for i in (1, 2, 3))
    collectors[owner].conflict_devices = {"d2"}
    await reconciler.reconcile()

    with pytest.raises(TaskPlacementUnsafeError, match="revision conflict"):
        await control.start_task("task-a")
    assert collectors[owner].started_tasks == ["d1"]
    assert reconciler.placement_safe is False


@pytest.mark.asyncio
async def test_start_task_generic_failure_does_not_report_success(tmp_path) -> None:
    """部分设备非冲突失败：异常上抛（HTTP 非 2xx），已启动设备保持事实记账。"""
    devices = {f"d{i}": _group_device(f"d{i}") for i in (1, 2)}
    config = _Config(tmp_path, ["task-a"], devices=devices)
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.extend(_instance(f"d{i}", "task-a") for i in (1, 2))
    collectors[owner].generic_fail_devices = {"d2"}
    await reconciler.reconcile()

    with pytest.raises(RuntimeError, match="start failed"):
        await control.start_task("task-a")
    assert collectors[owner].started_tasks == ["d1"]
    # 非冲突失败不关闭 placement 栅栏（只有 revision 冲突才关）。
    assert reconciler.placement_safe is True


@pytest.mark.asyncio
async def test_repeated_start_task_fans_out_each_time(tmp_path) -> None:
    """重复 start 是幂等重发：两次调用都按设备完整扇出，不丢设备。"""
    devices = {f"d{i}": _group_device(f"d{i}") for i in (1, 2)}
    config = _Config(tmp_path, ["task-a"], devices=devices)
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.extend(_instance(f"d{i}", "task-a") for i in (1, 2))
    await reconciler.reconcile()

    await control.start_task("task-a")
    await control.start_task("task-a")

    assert sorted(collectors[owner].started_tasks) == ["d1", "d1", "d2", "d2"]


def _task_cfg(task_id: str, group: str = "d1", *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        device_group_id=group,
        point_group="fast",
        interval=1.0,
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_start_disabled_task_is_rejected(tmp_path) -> None:
    """禁用 Task 的 start_task 必须在任何设备下发前抛 ValueError（HTTP 409）。"""
    config = _Config(tmp_path, ["task-a"], tasks={"task-a": _task_cfg("task-a", enabled=False)})
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("d1", "task-a"))
    await reconciler.reconcile()

    with pytest.raises(ValueError, match="disabled"):
        await control.start_task("task-a")
    assert collectors[owner].started_tasks == []


@pytest.mark.asyncio
async def test_start_instance_owned_only_by_disabled_task_is_rejected(tmp_path) -> None:
    """设备的全部归属 Task 均禁用时，start_instance 拒绝（防止共享设备绕过）。"""
    config = _Config(tmp_path, ["task-a"], tasks={"task-a": _task_cfg("task-a", enabled=False)})
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("d1", "task-a"))
    await reconciler.reconcile()

    with pytest.raises(ValueError, match="no enabled task"):
        await control.start_instance("d1")
    assert collectors[owner].started_instances == []


@pytest.mark.asyncio
async def test_start_instance_allowed_when_enabled_task_shares_device(tmp_path) -> None:
    """禁用 Task 与启用 Task 共享设备且 placement 在同一 Worker：允许启动。

    设备级采集只有一个实例，启用 Task 已合法拥有该设备；禁用 Task 无法借此
    绕过 start_task 守卫（其自身入口仍被拒绝）。
    """
    devices = {"d1": _group_device("d1")}
    config = _Config(
        tmp_path,
        ["task-a", "task-b"],
        devices=devices,
        tasks={
            "task-a": _task_cfg("task-a", enabled=True),
            "task-b": _task_cfg("task-b", enabled=False),
        },
    )
    collectors, placements, reconciler, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    assert placements.worker_for_task("task-b") == owner
    collectors[owner].instances.append(_instance("d1", "task-a"))
    await reconciler.reconcile()

    # 设备实例可由启用 Task 的归属启动。
    started = await control.start_instance("d1")
    assert started.state.value == "running"
    assert collectors[owner].started_instances == ["d1"]
    # 禁用 Task 自身的批量入口仍然拒绝，且未借共享设备下发。
    with pytest.raises(ValueError, match="disabled"):
        await control.start_task("task-b")


@pytest.mark.asyncio
async def test_stop_task_and_stop_instance_allowed_when_task_disabled(tmp_path) -> None:
    """STOP 不增加禁用限制：禁用 Task 仍可停止其设备。"""
    config = _Config(tmp_path, ["task-a"], tasks={"task-a": _task_cfg("task-a", enabled=False)})
    collectors, placements, _, control = _assemble_with(tmp_path, config, ["collector-a"])
    owner = placements.worker_for_task("task-a")
    collectors[owner].instances.append(_instance("d1", "task-a", state="running"))

    stopped = await control.stop_instance("d1")
    assert stopped.state.value == "stopped"
    summary = await control.stop_task("task-a")
    assert summary.task_id == "task-a"
    assert collectors[owner].stopped_instances == ["d1"]
    assert collectors[owner].stopped_tasks == ["d1"]
