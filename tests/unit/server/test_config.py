"""ConfigUseCase 配置事务编排单元测试。

Server 是全系统配置事务的唯一编排者：加载/校验/diff → 全参与者 Prepare →
全参与者 Activate → 失败时 Abort + 磁盘回滚 + Worker 强制收敛。这里用
可编程的假 Worker（Collector/Commander 出站端口）覆盖事务矩阵：成功、
无变更短路、prepare 失败、hash 不一致、activate 失败/结果未知、非法磁盘
配置、停机拒绝、reconcile 收敛与身份校验——全部在进程内、无网络。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.component.collector.conftest import update_yaml
from tests.support.config_helper import write_config_tree
from wind_hub_core.config.schema import Config
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_core.config.diff import compute_diff
from wind_hub_server.application.worker_model import COMMANDER_WORKER_ID

COLLECTOR_ID = "collector-1"


# ---------------------------------------------------------------------------
# 可编程假 Worker（同时扮演 Collector 与 Commander 出站端口）
# ---------------------------------------------------------------------------


class _FakeWorker:
    """状态化假 Worker：记录调用、按队列吐出预设结果、维护 active 配置状态。

    ``prepare_outcomes`` / ``activate_outcomes`` 是结果队列：每次调用弹出一项
    （空队列时默认成功）；项为 Exception 实例时模拟 RPC 异常抛出。成功
    prepare/activate 会更新假 Worker 的 active revision/hash——让 reconcile
    与「activate 后回查状态」路径可用自然状态断言。
    """

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.prepare_calls: list[tuple[str, str, bool]] = []
        self.activate_calls: list[str] = []
        self.abort_calls: list[str] = []
        self.prepare_outcomes: deque[Any] = deque()
        self.activate_outcomes: deque[Any] = deque()
        self.abort_outcomes: deque[Any] = deque()
        self.active_revision: str | None = None
        self.active_hash: str | None = None
        self.status_error: BaseException | None = None
        self._last_prepared_hash: str | None = None

    @staticmethod
    def _pop(queue: deque[Any], default: dict[str, Any]) -> Any:
        return queue.popleft() if queue else default

    async def config_status(self) -> dict[str, Any]:
        if self.status_error is not None:
            raise self.status_error
        return {
            "collector_id": self.worker_id,
            "active_revision": self.active_revision,
            "active_config_hash": self.active_hash,
        }

    # CommanderPort.status 与 CollectorPort.config_status 同构。
    status = config_status

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
        force_reconfigure: bool = False,
    ) -> dict[str, Any]:
        self.prepare_calls.append((revision_id, config_hash, force_reconfigure))
        outcome = self._pop(self.prepare_outcomes, {"success": True})
        if isinstance(outcome, BaseException):
            raise outcome
        result = dict(outcome)
        result.setdefault("config_hash", config_hash)
        if result.get("success"):
            self._last_prepared_hash = str(result["config_hash"])
        return result

    async def activate_config(self, revision_id: str) -> dict[str, Any]:
        self.activate_calls.append(revision_id)
        outcome = self._pop(self.activate_outcomes, {"success": True})
        if isinstance(outcome, BaseException):
            raise outcome
        result = dict(outcome)
        result.setdefault("active_config_hash", self._last_prepared_hash or "")
        if result.get("success"):
            self.active_revision = revision_id
            self.active_hash = str(result["active_config_hash"])
        return result

    async def abort_config(self, revision_id: str) -> dict[str, Any]:
        self.abort_calls.append(revision_id)
        outcome = self._pop(self.abort_outcomes, {"success": True})
        if isinstance(outcome, BaseException):
            raise outcome
        return dict(outcome)


class _FakeDirectory:
    """CollectorDirectory 假实现：按 worker_id 返回假 Collector。"""

    def __init__(self, workers: dict[str, _FakeWorker]) -> None:
        self._workers = workers

    def get(self, worker_id: str) -> _FakeWorker:
        return self._workers[worker_id]

    def list_worker_ids(self) -> list[str]:
        return sorted(self._workers)


# ---------------------------------------------------------------------------
# 配置现场与 UseCase 工厂
# ---------------------------------------------------------------------------


def _write_site(base: Path, *, interval: float = 0.2) -> Path:
    """写出最小合法配置集（单 Modbus 设备 + file sink + 单采集任务）。"""
    return write_config_tree(
        base,
        devices=[
            {
                "device_id": "modbus-1",
                "protocol": "modbus",
                "point_table": "modbus",
                "endpoint": {"host": "127.0.0.1", "port": 5020},
            }
        ],
        point_tables={
            "modbus": {
                "points": [
                    {
                        "point_id": "rotor.speed",
                        "point_groups": ["telemetry"],
                        "address": {"register_type": "holding", "address": 100},
                        "data_type": "float32",
                    }
                ]
            }
        },
        sinks=[{"name": "file_sink", "type": "file", "params": {"path": "out.jsonl"}}],
        tasks=[
            {
                "task_id": "modbus-telemetry",
                "device": "modbus-1",
                "point_group": "telemetry",
                "interval": interval,
                "targets": [{"sink": "file_sink"}],
            }
        ],
    )


class _Fixture:
    """一套 UseCase + 假 Worker + 配置目录的组合。"""

    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = _write_site(tmp_path / "cfg")
        self.collector = _FakeWorker(COLLECTOR_ID)
        self.commander = _FakeWorker(COMMANDER_WORKER_ID)
        self.directory = _FakeDirectory({COLLECTOR_ID: self.collector})
        self.usecase = ConfigUseCase(
            self.config_dir,
            self.directory,
            self.commander,
            ConfigUseCase.load_directory(self.config_dir),
        )

    @property
    def workers(self) -> list[_FakeWorker]:
        return [self.collector, self.commander]


@pytest.fixture
def fx(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


def _set_interval(config_dir: Path, interval: float) -> None:
    update_yaml(
        config_dir,
        "tasks.yaml",
        lambda data: data["tasks"][0].update({"interval": interval}),
    )


def _task_interval(config_dir: Path) -> float:
    data = yaml.safe_load((config_dir / "tasks.yaml").read_text(encoding="utf-8"))
    return data["tasks"][0]["interval"]


# ---------------------------------------------------------------------------
# 构造守卫
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_at_least_one_collector(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        with pytest.raises(ValueError, match="at least one collector"):
            ConfigUseCase(
                config_dir,
                _FakeDirectory({}),
                _FakeWorker(COMMANDER_WORKER_ID),
                ConfigUseCase.load_directory(config_dir),
            )

    def test_commander_id_conflict_rejected(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        with pytest.raises(ValueError, match="conflicts with commander"):
            ConfigUseCase(
                config_dir,
                _FakeDirectory({COMMANDER_WORKER_ID: _FakeWorker(COMMANDER_WORKER_ID)}),
                _FakeWorker(COMMANDER_WORKER_ID),
                ConfigUseCase.load_directory(config_dir),
            )


# ---------------------------------------------------------------------------
# reload 成功路径
# ---------------------------------------------------------------------------


class TestReloadSuccess:
    async def test_reload_prepares_and_activates_all_participants(
        self, fx: _Fixture
    ) -> None:
        _set_interval(fx.config_dir, 0.5)
        result = await fx.usecase.reload()

        assert result.success is True, result.errors
        assert result.diff.tasks.updated == ["modbus-telemetry"]
        # 全部参与者（collector + commander）按同 revision/hash 两阶段推进。
        for worker in fx.workers:
            assert len(worker.prepare_calls) == 1
            assert worker.activate_calls == [worker.prepare_calls[0][0]]
        assert fx.usecase.desired_revision == fx.collector.activate_calls[0]
        assert fx.usecase.desired_config_hash is not None

    async def test_unchanged_config_short_circuits_without_rpc(
        self, fx: _Fixture
    ) -> None:
        first = await fx.usecase.reload()
        assert first.success is True, first.errors
        for worker in fx.workers:
            worker.prepare_calls.clear()
            worker.activate_calls.clear()

        second = await fx.usecase.reload()

        assert second.success is True, second.errors
        assert second.diff.has_any_changes is False
        for worker in fx.workers:
            assert worker.prepare_calls == []
            assert worker.activate_calls == []

    async def test_first_reload_without_changes_does_not_touch_workers(
        self, fx: _Fixture
    ) -> None:
        """磁盘配置与基线一致时，首次 reload 同样短路（幂等 bootstrap）。"""
        result = await fx.usecase.reload()

        assert result.success is True, result.errors
        for worker in fx.workers:
            assert worker.prepare_calls == []

    async def test_force_workers_bypasses_short_circuit(self, fx: _Fixture) -> None:
        result = await fx.usecase.reload(force_workers=True)

        assert result.success is True, result.errors
        # force_reconfigure 必须透传到 Collector 侧 prepare（Commander 端口
        # 不接受 force 参数——其 prepare 语义见 CommanderPort 契约）。
        assert fx.collector.prepare_calls and all(
            force for _, _, force in fx.collector.prepare_calls
        )
        assert len(fx.commander.prepare_calls) == 1


# ---------------------------------------------------------------------------
# reload 失败路径：磁盘配置非法
# ---------------------------------------------------------------------------


class TestReloadInvalidDiskConfig:
    async def test_invalid_config_fails_and_restores_disk(self, fx: _Fixture) -> None:
        original_tasks = (fx.config_dir / "tasks.yaml").read_bytes()
        ghost = fx.config_dir / "ghost.yaml"
        update_yaml(
            fx.config_dir,
            "tasks.yaml",
            lambda data: data["tasks"][0].update({"targets": [{"sink": "ghost"}]}),
        )
        ghost.write_text("ghost: true\n", encoding="utf-8")

        result = await fx.usecase.reload()

        assert result.success is False
        assert result.errors
        # 失败必须回滚磁盘：tasks.yaml 恢复原内容，新增文件被删除。
        assert (fx.config_dir / "tasks.yaml").read_bytes() == original_tasks
        assert not ghost.exists()
        # 非法配置不得触及任何 Worker。
        for worker in fx.workers:
            assert worker.prepare_calls == []
            assert worker.activate_calls == []


# ---------------------------------------------------------------------------
# reload 失败路径：prepare 阶段
# ---------------------------------------------------------------------------


class TestPrepareFailures:
    async def test_prepare_failure_aborts_all_and_restores_disk(
        self, fx: _Fixture
    ) -> None:
        _set_interval(fx.config_dir, 0.9)
        fx.collector.prepare_outcomes.append(
            {"success": False, "errors": ["collector rejected"]}
        )

        result = await fx.usecase.reload()

        assert result.success is False
        assert any("collector rejected" in e for e in result.errors)
        # 全部参与者收到同 revision 的 Abort；无人收到 Activate。
        for worker in fx.workers:
            assert worker.abort_calls == [worker.prepare_calls[0][0]]
            assert worker.activate_calls == []
        # 磁盘回滚到最近成功基线。
        assert _task_interval(fx.config_dir) == 0.2
        assert fx.usecase.desired_revision is None

    async def test_prepare_hash_mismatch_treated_as_failure(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.9)
        fx.commander.prepare_outcomes.append(
            {"success": True, "config_hash": "tampered-hash"}
        )

        result = await fx.usecase.reload()

        assert result.success is False
        assert any("prepare hash mismatch" in e for e in result.errors)
        for worker in fx.workers:
            assert worker.activate_calls == []

    async def test_collector_identity_mismatch_fails_reload(self, fx: _Fixture) -> None:
        """Collector 上报身份与逻辑 worker_id 不一致：事务不得继续。"""
        _set_interval(fx.config_dir, 0.9)
        fx.collector.worker_id = "someone-else"

        result = await fx.usecase.reload()

        assert result.success is False
        assert any("identity mismatch" in e for e in result.errors)


# ---------------------------------------------------------------------------
# reload 失败路径：activate 阶段
# ---------------------------------------------------------------------------


class TestActivateFailures:
    async def test_activate_failure_aborts_and_rolls_back(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.7)
        fx.commander.activate_outcomes.append({"success": False, "errors": ["boom"]})

        result = await fx.usecase.reload()

        assert result.success is False
        assert any("boom" in e for e in result.errors)
        for worker in fx.workers:
            assert worker.abort_calls, "failed revision must be aborted"
        # 磁盘回滚 + Worker 强制收敛到最近成功配置（rollback revision 重新
        # prepare/activate；force_reconfigure 只透传到 Collector 端口）。
        assert _task_interval(fx.config_dir) == 0.2
        assert any(force for _, _, force in fx.collector.prepare_calls[1:])
        assert len(fx.commander.prepare_calls) >= 2
        assert fx.usecase.desired_revision is None

    async def test_activate_rpc_error_confirmed_by_status_is_success(
        self, fx: _Fixture
    ) -> None:
        """Activate RPC 异常但状态回查确认已生效 → 按成功收敛（结果未知≠失败）。"""
        _set_interval(fx.config_dir, 0.7)

        async def _activate_then_lose(revision_id: str) -> dict[str, Any]:
            # 模拟 Worker 已生效但响应丢失：状态已翻转到新 revision。
            fx.commander.active_revision = revision_id
            fx.commander.active_hash = fx.commander._last_prepared_hash
            raise TimeoutError("activate response lost")

        fx.commander.activate_config = _activate_then_lose  # type: ignore[method-assign]

        result = await fx.usecase.reload()

        assert result.success is True, result.errors
        assert fx.usecase.desired_revision is not None

    async def test_activate_rpc_error_unconfirmed_rolls_back(self, fx: _Fixture) -> None:
        """Activate RPC 异常且状态回查未生效 → 失败并回滚。"""
        _set_interval(fx.config_dir, 0.7)
        fx.commander.activate_outcomes.append(TimeoutError("activate response lost"))

        result = await fx.usecase.reload()

        assert result.success is False
        assert any("activate failed after RPC error" in e for e in result.errors)
        assert _task_interval(fx.config_dir) == 0.2


# ---------------------------------------------------------------------------
# 停机语义
# ---------------------------------------------------------------------------


class TestShutdownSemantics:
    async def test_reload_rejected_after_transactions_closed(self, fx: _Fixture) -> None:
        fx.usecase.stop_accepting_transactions()

        result = await fx.usecase.reload()

        assert result.success is False
        assert result.errors == ["config transactions are shutting down"]
        for worker in fx.workers:
            assert worker.prepare_calls == []

    async def test_wait_for_transactions_returns_true_when_idle(
        self, fx: _Fixture
    ) -> None:
        assert await fx.usecase.wait_for_transactions(timeout=1.0) is True


# ---------------------------------------------------------------------------
# reconcile_workers
# ---------------------------------------------------------------------------


class TestReconcileWorkers:
    async def test_no_desired_revision_reports_for_all(self, fx: _Fixture) -> None:
        outcomes = await fx.usecase.reconcile_workers()

        assert outcomes == {
            COLLECTOR_ID: "no-desired-revision",
            COMMANDER_WORKER_ID: "no-desired-revision",
        }

    async def test_already_current_after_successful_reload(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.usecase.reload()).success is True
        for worker in fx.workers:
            worker.prepare_calls.clear()

        outcomes = await fx.usecase.reconcile_workers()

        assert outcomes == {
            COLLECTOR_ID: "already-current",
            COMMANDER_WORKER_ID: "already-current",
        }
        for worker in fx.workers:
            assert worker.prepare_calls == []

    async def test_drifted_worker_is_reconciled_with_force(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.usecase.reload()).success is True
        # 模拟 Worker 偏离：active 状态落后（如 Worker 重启丢了配置）。
        fx.collector.active_revision = "stale"
        fx.collector.active_hash = "stale"
        fx.collector.prepare_calls.clear()

        outcomes = await fx.usecase.reconcile_workers()

        assert outcomes[COLLECTOR_ID] == "reconciled"
        assert outcomes[COMMANDER_WORKER_ID] == "already-current"
        revision, _, force = fx.collector.prepare_calls[0]
        assert revision == fx.usecase.desired_revision
        assert force is True

    async def test_status_error_reported_per_worker(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.usecase.reload()).success is True
        fx.collector.status_error = ConnectionError("unreachable")

        outcomes = await fx.usecase.reconcile_workers()

        assert outcomes[COLLECTOR_ID].startswith("status-error:")
        assert outcomes[COMMANDER_WORKER_ID] == "already-current"

    async def test_reconcile_rejected_after_transactions_closed(
        self, fx: _Fixture
    ) -> None:
        fx.usecase.stop_accepting_transactions()

        outcomes = await fx.usecase.reconcile_workers()

        assert set(outcomes.values()) == {"transactions-closed"}


# ---------------------------------------------------------------------------
# initialize_desired_revision（bootstrap）
# ---------------------------------------------------------------------------


class TestInitializeDesiredRevision:
    def test_bootstrap_revision_from_stable_disk(self, fx: _Fixture) -> None:
        fx.usecase.initialize_desired_revision()

        assert fx.usecase.desired_revision is not None
        assert fx.usecase.desired_revision.startswith("bootstrap-")
        assert fx.usecase.desired_config_hash is not None

    def test_disk_diverged_from_baseline_raises(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 3.3)

        with pytest.raises(ValueError, match="differs from disk"):
            fx.usecase.initialize_desired_revision()


# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------


def _load(config_dir: Path) -> Config:
    return ConfigUseCase.load_directory(config_dir)


class TestComputeDiff:
    def test_no_changes(self, tmp_path: Path) -> None:
        config = _load(_write_site(tmp_path / "cfg"))
        diff = compute_diff(config, config)

        assert diff.has_any_changes is False
        assert diff.devices.unchanged == ["modbus-1"]
        assert diff.tasks.unchanged == ["modbus-telemetry"]

    def test_task_update_and_point_table_change(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        old = _load(config_dir)
        _set_interval(config_dir, 1.0)
        update_yaml(
            config_dir,
            "points.yaml",
            lambda data: data["point_tables"]["modbus"]["points"].append(
                {
                    "point_id": "gen.power",
                    "point_groups": ["telemetry"],
                    "address": {"register_type": "holding", "address": 102},
                    "data_type": "float32",
                }
            ),
        )
        new = _load(config_dir)

        diff = compute_diff(old, new)

        assert diff.has_any_changes is True
        assert diff.tasks.updated == ["modbus-telemetry"]
        assert diff.points_changed is True
        assert diff.point_tables_changed == ["modbus"]
        assert diff.devices.unchanged == ["modbus-1"]

    def test_device_added_and_removed(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        old = _load(config_dir)

        def _add_device(data: dict) -> None:
            # devices.yaml 存放实例（引用 device_models 中的 model）——复制
            # 现有实例并改 id/端口，保持 model 引用有效。
            instance = dict(data["devices"][0])
            instance["device_id"] = "modbus-2"
            instance["endpoint"] = {**instance["endpoint"], "port": 5021}
            data["devices"].append(instance)

        update_yaml(config_dir, "devices.yaml", _add_device)
        new = _load(config_dir)

        diff = compute_diff(old, new)

        assert diff.devices.added == ["modbus-2"]
        assert diff.devices.removed == []
