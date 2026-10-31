"""ConfigService 配置事务编排单元测试（v2 参与者语义）。

v2 下 Collector 自行管理 Service / Instance / Template 三路径配置（五
Config RPC + 乐观 revision），不再参与 Server 的两段式事务；Server 的
ConfigService 只对 Commander 编排：加载/校验/diff → Prepare → Activate →
失败时 Abort + 磁盘回滚 + 强制收敛。Collector 目录仍是构造必需（至少一个、
身份不得与 Commander 冲突），但不收到任何配置事务 RPC。

可编程假 Commander 覆盖事务矩阵：成功、无变更短路、prepare 失败、hash
不一致、activate 失败/结果未知、非法磁盘配置、停机拒绝、reconcile 收敛
——全部在进程内、无网络。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest
import yaml

from core.application.config_diff import diff_config_snapshots
from core.application.config_snapshot import ConfigSnapshot
from tests.support.config_helper import write_config_tree
from tests.support.functional_config import update_yaml
from web_server.application.config.service import ConfigService
from web_server.application.port.worker import (
    CollectorInfo,
    CommanderStatus,
    ConfigAbortAck,
    ConfigActivateAck,
    ConfigPrepareAck,
)
from web_server.application.worker.model import COMMANDER_WORKER_ID

pytestmark = pytest.mark.unit

COLLECTOR_ID = "collector-1"


# ---------------------------------------------------------------------------
# 可编程假 Worker
# ---------------------------------------------------------------------------


class _FakeCommander:
    """状态化假 Commander：记录调用、按队列吐出预设结果、维护 active 状态。

    ``prepare_outcomes`` / ``activate_outcomes`` 是结果队列：每次调用弹出一项
    （空队列时默认成功）；项为 Exception 实例时模拟 RPC 异常抛出。成功
    prepare/activate 会更新假 Worker 的 active revision/hash——让 reconcile
    与「activate 后回查状态」路径可用自然状态断言。
    """

    def __init__(self) -> None:
        self.worker_id = COMMANDER_WORKER_ID
        self.prepare_calls: list[tuple[str, str]] = []
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

    async def status(self) -> CommanderStatus:
        if self.status_error is not None:
            raise self.status_error
        return CommanderStatus(
            running=True,
            device_count=0,
            active_revision=self.active_revision or "",
            active_config_hash=self.active_hash or "",
            prepared_revision=None,
        )

    async def prepare_config(
        self,
        revision_id: str,
        config_hash: str,
    ) -> ConfigPrepareAck:
        self.prepare_calls.append((revision_id, config_hash))
        outcome = self._pop(self.prepare_outcomes, {"success": True})
        if isinstance(outcome, BaseException):
            raise outcome
        result = dict(outcome)
        result.setdefault("config_hash", config_hash)
        if result.get("success"):
            self._last_prepared_hash = str(result["config_hash"])
        return ConfigPrepareAck(
            success=bool(result.get("success")),
            revision_id=revision_id,
            config_hash=str(result["config_hash"]),
            errors=[str(item) for item in result.get("errors", [])],
        )

    async def activate_config(self, revision_id: str) -> ConfigActivateAck:
        self.activate_calls.append(revision_id)
        outcome = self._pop(self.activate_outcomes, {"success": True})
        if isinstance(outcome, BaseException):
            raise outcome
        result = dict(outcome)
        result.setdefault("active_config_hash", self._last_prepared_hash or "")
        if result.get("success"):
            self.active_revision = revision_id
            self.active_hash = str(result["active_config_hash"])
        return ConfigActivateAck(
            success=bool(result.get("success")),
            revision_id=revision_id,
            active_config_hash=str(result["active_config_hash"]),
            errors=[str(item) for item in result.get("errors", [])],
        )

    async def abort_config(self, revision_id: str) -> ConfigAbortAck:
        self.abort_calls.append(revision_id)
        outcome = self._pop(self.abort_outcomes, {"success": True})
        if isinstance(outcome, BaseException):
            raise outcome
        success = bool(outcome.get("success"))
        return ConfigAbortAck(
            success=success,
            revision_id=revision_id,
            aborted=success,
        )


class _FakeCollector:
    """Collector 替身：ConfigService 不得对它发起任何配置事务调用。"""

    def __init__(self, worker_id: str = COLLECTOR_ID) -> None:
        self.worker_id = worker_id

    async def config_status(self) -> CollectorInfo:
        return CollectorInfo(
            collector_id=self.worker_id,
            boot_id="boot-1",
            active_revision="",
            runtime_running=True,
        )


class _FakeDirectory:
    """CollectorDirectory 假实现：按 worker_id 返回假 Collector。"""

    def __init__(self, collectors: dict[str, _FakeCollector]) -> None:
        self._collectors = collectors

    def get(self, worker_id: str) -> _FakeCollector:
        return self._collectors[worker_id]

    def list_worker_ids(self) -> list[str]:
        return sorted(self._collectors)


# ---------------------------------------------------------------------------
# 配置现场与 Service 工厂
# ---------------------------------------------------------------------------


def _write_site(base: Path, *, interval: float = 0.2) -> Path:
    """写出最小合法配置集（单 Modbus 设备 + file sink + 单采集任务）。"""
    return write_config_tree(
        base,
        devices={
            "modbus-1": {
                "protocol": "modbus",
                "point_table": "modbus",
                "endpoint": {"host": "127.0.0.1", "port": 5020},
            }
        },
        point_tables={
            "modbus": {
                "points": {
                    "rotor_speed": {
                        "point_groups": ["telemetry"],
                        "address": {"register_type": "holding", "address": 100},
                        "data_type": "float32",
                    }
                }
            }
        },
        sinks={
            "redis_latest": {
                "type": "redis",
                "connection": {"host": "127.0.0.1"},
                "outputs": {"modbus-telemetry": {}},
            }
        },
        tasks={
            "modbus-telemetry": {
                "device_group_id": "modbus-1",
                "point_group": "telemetry",
                "interval": interval,
            }
        },
    )


class _Fixture:
    """一套 Service + 假 Commander + 假 Collector + 配置目录的组合。"""

    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = _write_site(tmp_path / "cfg")
        self.collector = _FakeCollector(COLLECTOR_ID)
        self.commander = _FakeCommander()
        self.directory = _FakeDirectory({COLLECTOR_ID: self.collector})
        self.service = ConfigService(
            self.config_dir,
            self.directory,
            self.commander,
            ConfigService.load_directory(self.config_dir),
        )


@pytest.fixture
def fx(tmp_path: Path) -> _Fixture:
    return _Fixture(tmp_path)


def _set_interval(config_dir: Path, interval: float) -> None:
    update_yaml(
        config_dir,
        "tasks.yaml",
        lambda data: data["tasks"]["modbus-telemetry"].update({"interval": interval}),
    )


def _task_interval(config_dir: Path) -> float:
    data = yaml.safe_load((config_dir / "tasks.yaml").read_text(encoding="utf-8"))
    return data["tasks"]["modbus-telemetry"]["interval"]


# ---------------------------------------------------------------------------
# 构造守卫
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_requires_at_least_one_collector(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        with pytest.raises(ValueError, match="at least one collector"):
            ConfigService(
                config_dir,
                _FakeDirectory({}),
                _FakeCommander(),
                ConfigService.load_directory(config_dir),
            )

    def test_commander_id_conflict_rejected(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        with pytest.raises(ValueError, match="conflicts with commander"):
            ConfigService(
                config_dir,
                _FakeDirectory({COMMANDER_WORKER_ID: _FakeCollector(COMMANDER_WORKER_ID)}),
                _FakeCommander(),
                ConfigService.load_directory(config_dir),
            )


# ---------------------------------------------------------------------------
# reload 成功路径
# ---------------------------------------------------------------------------


class TestReloadSuccess:
    async def test_reload_prepares_and_activates_commander(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.5)
        result = await fx.service.reload()

        assert result.success is True, result.errors
        assert sorted(result.diff.sections["tasks"].changed) == ["modbus-telemetry"]
        # v2 唯一事务参与者是 Commander；Collector 不收任何事务 RPC。
        assert len(fx.commander.prepare_calls) == 1
        assert fx.commander.activate_calls == [fx.commander.prepare_calls[0][0]]
        assert fx.service.desired_revision == fx.commander.activate_calls[0]
        assert fx.service.desired_config_hash is not None

    async def test_unchanged_config_short_circuits_without_rpc(self, fx: _Fixture) -> None:
        first = await fx.service.reload()
        assert first.success is True, first.errors
        fx.commander.prepare_calls.clear()
        fx.commander.activate_calls.clear()

        second = await fx.service.reload()

        assert second.success is True, second.errors
        assert second.diff.has_changes is False
        assert fx.commander.prepare_calls == []
        assert fx.commander.activate_calls == []

    async def test_first_reload_without_changes_does_not_touch_commander(
        self, fx: _Fixture
    ) -> None:
        """磁盘配置与基线一致时，首次 reload 同样短路（幂等 bootstrap）。"""
        result = await fx.service.reload()

        assert result.success is True, result.errors
        assert fx.commander.prepare_calls == []

    async def test_force_bypasses_short_circuit(self, fx: _Fixture) -> None:
        """force_workers 下即便无 diff 也要对 Commander 执行一次事务。"""
        result = await fx.service.reload(force_workers=True)

        assert result.success is True, result.errors
        assert len(fx.commander.prepare_calls) == 1
        assert len(fx.commander.activate_calls) == 1


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
            lambda data: data["tasks"]["modbus-telemetry"].update(
                {"outputs": [{"sink_id": "ghost"}]}
            ),
        )
        ghost.write_text("ghost: true\n", encoding="utf-8")

        result = await fx.service.reload()

        assert result.success is False
        assert result.errors
        # 失败必须回滚磁盘：tasks.yaml 恢复原内容，新增文件被删除。
        assert (fx.config_dir / "tasks.yaml").read_bytes() == original_tasks
        assert not ghost.exists()
        # 非法配置不得触及 Commander。
        assert fx.commander.prepare_calls == []
        assert fx.commander.activate_calls == []


# ---------------------------------------------------------------------------
# reload 失败路径：prepare 阶段
# ---------------------------------------------------------------------------


class TestPrepareFailures:
    async def test_prepare_failure_aborts_and_restores_disk(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.9)
        fx.commander.prepare_outcomes.append({"success": False, "errors": ["commander rejected"]})

        result = await fx.service.reload()

        assert result.success is False
        assert any("commander rejected" in e for e in result.errors)
        assert fx.commander.abort_calls == [fx.commander.prepare_calls[0][0]]
        assert fx.commander.activate_calls == []
        # 磁盘回滚到最近成功基线。
        assert _task_interval(fx.config_dir) == 0.2
        assert fx.service.desired_revision is None

    async def test_prepare_hash_mismatch_treated_as_failure(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.9)
        fx.commander.prepare_outcomes.append({"success": True, "config_hash": "tampered-hash"})

        result = await fx.service.reload()

        assert result.success is False
        assert any("prepare hash mismatch" in e for e in result.errors)
        assert fx.commander.activate_calls == []


# ---------------------------------------------------------------------------
# reload 失败路径：activate 阶段
# ---------------------------------------------------------------------------


class TestActivateFailures:
    async def test_activate_failure_aborts_and_rolls_back(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.7)
        fx.commander.activate_outcomes.append({"success": False, "errors": ["boom"]})

        result = await fx.service.reload()

        assert result.success is False
        assert any("boom" in e for e in result.errors)
        assert fx.commander.abort_calls, "failed revision must be aborted"
        # 磁盘回滚 + Commander 强制收敛到最近成功配置（回滚事务再次
        # prepare/activate）。
        assert _task_interval(fx.config_dir) == 0.2
        assert len(fx.commander.prepare_calls) >= 2
        assert fx.service.desired_revision is None

    async def test_activate_rpc_error_confirmed_by_status_is_success(self, fx: _Fixture) -> None:
        """Activate RPC 异常但状态回查确认已生效 → 按成功收敛（结果未知≠失败）。"""
        _set_interval(fx.config_dir, 0.7)

        async def _activate_then_lose(revision_id: str) -> dict[str, Any]:
            # 模拟已生效但响应丢失：状态已翻转到新 revision。
            fx.commander.active_revision = revision_id
            fx.commander.active_hash = fx.commander._last_prepared_hash
            raise TimeoutError("activate response lost")

        fx.commander.activate_config = _activate_then_lose  # type: ignore[method-assign]

        result = await fx.service.reload()

        assert result.success is True, result.errors
        assert fx.service.desired_revision is not None

    async def test_activate_rpc_error_unconfirmed_rolls_back(self, fx: _Fixture) -> None:
        """Activate RPC 异常且状态回查未生效 → 失败并回滚。"""
        _set_interval(fx.config_dir, 0.7)
        fx.commander.activate_outcomes.append(TimeoutError("activate response lost"))

        result = await fx.service.reload()

        assert result.success is False
        assert any("activate failed after RPC error" in e for e in result.errors)
        assert _task_interval(fx.config_dir) == 0.2


# ---------------------------------------------------------------------------
# 停机语义
# ---------------------------------------------------------------------------


class TestShutdownSemantics:
    async def test_reload_rejected_after_transactions_closed(self, fx: _Fixture) -> None:
        fx.service.stop_accepting_transactions()

        result = await fx.service.reload()

        assert result.success is False
        assert result.errors == ["config transactions are shutting down"]
        assert fx.commander.prepare_calls == []

    async def test_wait_for_transactions_returns_true_when_idle(self, fx: _Fixture) -> None:
        assert await fx.service.wait_for_transactions(timeout=1.0) is True


# ---------------------------------------------------------------------------
# reconcile_workers（v2 只收敛 Commander）
# ---------------------------------------------------------------------------


class TestReconcileWorkers:
    async def test_no_desired_revision_reports_for_commander(self, fx: _Fixture) -> None:
        outcomes = await fx.service.reconcile_workers()

        assert outcomes == {COMMANDER_WORKER_ID: "no-desired-revision"}

    async def test_already_current_after_successful_reload(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.service.reload()).success is True
        fx.commander.prepare_calls.clear()

        outcomes = await fx.service.reconcile_workers()

        assert outcomes == {COMMANDER_WORKER_ID: "already-current"}
        assert fx.commander.prepare_calls == []

    async def test_drifted_commander_is_reconciled(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.service.reload()).success is True
        # 模拟 Commander 偏离：active 状态落后（如重启丢了配置）。
        fx.commander.active_revision = "stale"
        fx.commander.active_hash = "stale"
        fx.commander.prepare_calls.clear()

        outcomes = await fx.service.reconcile_workers()

        assert outcomes[COMMANDER_WORKER_ID] == "reconciled"
        revision, _ = fx.commander.prepare_calls[0]
        assert revision == fx.service.desired_revision

    async def test_status_error_reported(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.service.reload()).success is True
        fx.commander.status_error = ConnectionError("unreachable")

        outcomes = await fx.service.reconcile_workers()

        assert outcomes[COMMANDER_WORKER_ID].startswith("status-error:")

    async def test_reconcile_rejected_after_transactions_closed(self, fx: _Fixture) -> None:
        fx.service.stop_accepting_transactions()

        outcomes = await fx.service.reconcile_workers()

        assert set(outcomes.values()) == {"transactions-closed"}


# ---------------------------------------------------------------------------
# initialize_desired_revision（bootstrap）
# ---------------------------------------------------------------------------


class TestInitializeDesiredRevision:
    def test_bootstrap_revision_from_stable_disk(self, fx: _Fixture) -> None:
        fx.service.initialize_desired_revision()

        assert fx.service.desired_revision is not None
        assert fx.service.desired_revision.startswith("bootstrap-")
        assert fx.service.desired_config_hash is not None

    def test_disk_diverged_from_baseline_raises(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 3.3)

        with pytest.raises(ValueError, match="differs from disk"):
            fx.service.initialize_desired_revision()


# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------


def _load(config_dir: Path) -> ConfigSnapshot:
    return ConfigService.load_directory(config_dir)


class TestComputeDiff:
    def test_no_changes(self, tmp_path: Path) -> None:
        config = _load(_write_site(tmp_path / "cfg"))
        diff = diff_config_snapshots(config, config)

        assert diff.has_changes is False
        assert diff.sections["devices"].has_changes is False
        assert diff.sections["tasks"].has_changes is False

    def test_task_update_and_point_table_change(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        old = _load(config_dir)
        _set_interval(config_dir, 1.0)
        update_yaml(
            config_dir,
            "points.yaml",
            lambda data: data["point_tables"]["modbus"]["points"].update(
                {
                    "gen_power": {
                        "point_groups": ["telemetry"],
                        "address": {"register_type": "holding", "address": 102},
                        "data_type": "float32",
                    }
                }
            ),
        )
        update_yaml(
            config_dir,
            "business_points.yaml",
            lambda data: data["business_points"].update(
                {"gen_power": {"data_type": "float32", "unit": "none"}}
            ),
        )
        new = _load(config_dir)

        diff = diff_config_snapshots(old, new)

        assert diff.has_changes is True
        assert sorted(diff.sections["tasks"].changed) == ["modbus-telemetry"]
        assert diff.sections["point_tables"].has_changes is True
        assert sorted(diff.sections["point_tables"].changed) == ["modbus"]
        assert diff.sections["devices"].has_changes is False

    def test_device_added_and_removed(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        old = _load(config_dir)

        def _add_device(data: dict) -> None:
            # devices.yaml 存放实例（引用 device_models 中的 model）——复制
            # 现有实例并改 id/端口，保持 model 引用有效。
            instance = dict(data["devices"]["modbus-1"])
            instance["endpoint"] = {**instance["endpoint"], "port": 5021}
            instance["device_group_ids"] = ["modbus-2"]
            data["devices"]["modbus-2"] = instance
            data["device_groups"]["modbus-2"] = {"name": "modbus-2"}

        update_yaml(config_dir, "devices.yaml", _add_device)
        new = _load(config_dir)

        diff = diff_config_snapshots(old, new)

        assert sorted(diff.sections["devices"].added) == ["modbus-2"]
        assert diff.sections["devices"].removed == frozenset()


async def test_concurrent_reloads_are_serialized(fx: _Fixture) -> None:
    """并发 reload 由事务锁串行化：只有一次 prepare/activate 事务。

    第二个 reload 在锁内看到第一个已应用的结果并无变更短路——绝不出现
    两个事务交错或 Commander 收到重复 revision 的情况。
    """
    import asyncio

    _set_interval(fx.config_dir, 0.5)

    first, second = await asyncio.gather(fx.service.reload(), fx.service.reload())

    assert first.success and second.success
    assert len(fx.commander.prepare_calls) == 1
    assert len(fx.commander.activate_calls) == 1


class TestCommitHooks:
    """成功提交后的副作用回调契约。"""

    async def test_hook_receives_previous_and_candidate(self, fx: _Fixture) -> None:
        calls: list[tuple[ConfigSnapshot, ConfigSnapshot]] = []
        fx.service.add_commit_hook(lambda previous, candidate: calls.append((previous, candidate)))

        _set_interval(fx.config_dir, 0.5)
        result = await fx.service.reload()

        assert result.success is True, result.errors
        assert len(calls) == 1
        previous, candidate = calls[0]
        task = "modbus-telemetry"
        assert previous.tasks[task].interval == 0.2
        assert candidate.tasks[task].interval == 0.5
        assert fx.service.current_config is candidate

    async def test_failing_hook_does_not_break_commit_or_other_hooks(
        self,
        fx: _Fixture,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        reached: list[bool] = []

        def boom(previous: ConfigSnapshot, candidate: ConfigSnapshot) -> None:
            raise RuntimeError("hook boom")

        fx.service.add_commit_hook(boom)
        fx.service.add_commit_hook(lambda previous, candidate: reached.append(True))

        _set_interval(fx.config_dir, 0.5)
        with caplog.at_level("WARNING"):
            result = await fx.service.reload()

        assert result.success is True, result.errors
        assert reached == [True]
        assert "hook boom" in caplog.text
        # 基线仍然完成提交。
        assert fx.service.current_config.tasks["modbus-telemetry"].interval == 0.5

    async def test_hooks_skip_no_change_short_circuit(self, fx: _Fixture) -> None:
        """无变更短路不经过 _commit_revision：钩子不应触发。"""
        calls = 0

        def counting(previous: ConfigSnapshot, candidate: ConfigSnapshot) -> None:
            nonlocal calls
            calls += 1

        fx.service.add_commit_hook(counting)
        result = await fx.service.reload()

        assert result.success is True
        assert result.diff.has_changes is False
        assert calls == 0
