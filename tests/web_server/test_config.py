"""ConfigService 配置事务编排单元测试（Commander v1 正式快照协议）。

N-03 后 Web Server 不再持有两段式 Prepare/Activate/Abort 事务，也不回滚
磁盘：配置变更统一走 Commander CommonService 的快照三方法——
GetConfigSnapshot("disk") → PushConfigSnapshot（乐观 revision + SHA-256）
→ ApplyConfigSnapshot → 回查 disk 提交与内容。任何 RPC 不确定都只报告
"not confirmed"，绝不自动重试 Apply。Collector 目录仍是构造必需（至少
一个、身份不得与 Commander 冲突），但不收到任何配置事务 RPC。

可编程假 Commander 在进程内模拟正式协议的状态机（disk/runtime 双
revision、候选验收、Apply 落盘），覆盖：成功事务、无变更短路、
force 穿透、本地非法配置 fail-closed、旧平铺配置越界 fail-closed、
Push 拒绝/基 revision 不符、Apply 失败/RPC 未知/回查不符、停机拒绝、
reconcile 只读取证——全部无网络。
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest
import yaml

from web_server.application.config.diff import diff_config_snapshots
from web_server.application.config.snapshot import ConfigSnapshot
from tests.support.config_helper import write_config_tree
from tests.support.functional_config import update_yaml
from web_server.application.config.service import ConfigService
from web_server.application.port.worker import (
    CollectorInfo,
    CommanderStatus,
    ConfigApplyResult,
    ConfigPushResult,
    RemoteConfigSnapshot,
)
from web_server.application.worker.model import COMMANDER_WORKER_ID

pytestmark = pytest.mark.unit

COLLECTOR_ID = "collector-1"


# ---------------------------------------------------------------------------
# 可编程假 Worker
# ---------------------------------------------------------------------------


class _FakeCommander:
    """状态化假 Commander：实现正式快照四方法的进程内状态机。

    - disk 状态：``disk_revision`` + ``disk_files``（键为相对配置根的
      posix 路径，与生产 SnapshotFile 合同一致）。
    - Push 携带 expected_revision 做乐观并发校验，产出候选；Apply 把候选
      落盘并推进 disk/runtime 两个 revision，供服务端回查确认。
    - ``push_outcomes`` / ``apply_outcomes`` 为结果队列：每项为 Exception
      时模拟 RPC 异常，否则为覆盖默认成功响应的 dict。
    """

    def __init__(self, config_dir: Path) -> None:
        self.worker_id = COMMANDER_WORKER_ID
        self._config_dir = config_dir
        self.disk_revision = "disk-rev-1"
        self.runtime_revision = "runtime-rev-1"
        self.disk_files = self._snapshot_remote_files()
        self.restart_required = False
        self.running = True
        self.status_error: BaseException | None = None
        self.push_outcomes: deque[Any] = deque()
        self.apply_outcomes: deque[Any] = deque()
        self.confirm_mismatch = False
        self.push_calls: list[tuple[dict[str, bytes], str]] = []
        self.apply_calls: list[tuple[str, str]] = []
        self.snapshot_scopes: list[str] = []
        self._candidates: dict[str, dict[str, bytes]] = {}
        self._serial = 1

    def _snapshot_remote_files(self) -> dict[str, bytes]:
        root = self._config_dir.parent
        files: dict[str, bytes] = {}
        for path in sorted(self._config_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}:
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        return files

    @staticmethod
    def _pop(queue: deque[Any], default: dict[str, Any]) -> Any:
        return queue.popleft() if queue else default

    async def status(self) -> CommanderStatus:
        if self.status_error is not None:
            raise self.status_error
        return CommanderStatus(
            running=self.running,
            device_count=0,
            active_revision=self.runtime_revision,
            # CommonService 不承诺旧版 active_config_hash；正式适配器恒为 ""。
            active_config_hash="",
            prepared_revision=None,
        )

    async def get_config_snapshot(self, scope: str) -> RemoteConfigSnapshot:
        self.snapshot_scopes.append(scope)
        return RemoteConfigSnapshot(
            revision=self.disk_revision,
            files=dict(self.disk_files),
        )

    async def push_config(
        self, files: dict[str, bytes], expected_revision: str
    ) -> ConfigPushResult:
        self.push_calls.append((dict(files), expected_revision))
        outcome = self._pop(self.push_outcomes, {"valid": True})
        if isinstance(outcome, BaseException):
            raise outcome
        result = dict(outcome)
        valid = bool(result.get("valid", True))
        errors = [str(item) for item in result.get("errors", [])]
        if not valid:
            return ConfigPushResult(
                candidate_id="",
                valid=False,
                base_revision=self.disk_revision,
                errors=errors,
            )
        candidate_id = f"candidate-{self._serial}"
        self._serial += 1
        self._candidates[candidate_id] = dict(files)
        return ConfigPushResult(
            candidate_id=str(result.get("candidate_id", candidate_id)),
            valid=True,
            base_revision=str(result.get("base_revision", self.disk_revision)),
            errors=errors,
        )

    async def apply_config(self, candidate_id: str, expected_revision: str) -> ConfigApplyResult:
        self.apply_calls.append((candidate_id, expected_revision))
        outcome = self._pop(self.apply_outcomes, {"ok": True})
        if isinstance(outcome, BaseException):
            raise outcome
        result = dict(outcome)
        errors = [str(item) for item in result.get("errors", [])]
        if errors:
            # 线上部分失败时响应仍携带 revision 字段；服务端据 errors 判收敛失败。
            return ConfigApplyResult(
                file_commit=str(result.get("file_commit", "disk-rev-partial")),
                effective_revision=str(result.get("effective_revision", "runtime-rev-partial")),
                restart_required=False,
                errors=errors,
            )
        files = self._candidates.pop(candidate_id, {})
        self.disk_files.update(files)
        self._serial += 1
        file_commit = str(result.get("file_commit", f"disk-rev-{self._serial}"))
        effective = str(result.get("effective_revision", f"runtime-rev-{self._serial}"))
        self.disk_revision = file_commit
        self.runtime_revision = effective
        if self.confirm_mismatch:
            # 模拟 Apply 落盘内容与候选不一致：回查必须识破。
            self.disk_revision = "disk-rev-unexpected"
        return ConfigApplyResult(
            file_commit=file_commit,
            effective_revision=effective,
            restart_required=bool(result.get("restart_required", self.restart_required)),
            errors=errors,
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
        self.commander = _FakeCommander(self.config_dir)
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
    return float(data["tasks"]["modbus-telemetry"]["interval"])


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
                _FakeCommander(config_dir),
                ConfigService.load_directory(config_dir),
            )

    def test_commander_id_conflict_rejected(self, tmp_path: Path) -> None:
        config_dir = _write_site(tmp_path / "cfg")
        with pytest.raises(ValueError, match="conflicts with Commander"):
            ConfigService(
                config_dir,
                _FakeDirectory({COMMANDER_WORKER_ID: _FakeCollector(COMMANDER_WORKER_ID)}),
                _FakeCommander(config_dir),
                ConfigService.load_directory(config_dir),
            )


# ---------------------------------------------------------------------------
# reload 成功路径
# ---------------------------------------------------------------------------


class TestReloadSuccess:
    async def test_reload_pushes_snapshot_and_applies(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.5)
        result = await fx.service.reload()

        assert result.success is True, result.errors
        assert sorted(result.diff.sections["tasks"].changed) == ["modbus-telemetry"]
        # 正式快照事务：一次 disk 回查（前置）+ Push + Apply + 一次确认回查。
        assert len(fx.commander.push_calls) == 1
        assert len(fx.commander.apply_calls) == 1
        pushed_files, expected_revision = fx.commander.push_calls[0]
        assert expected_revision == "disk-rev-1"
        assert "cfg/tasks.yaml" in pushed_files
        candidate_id, apply_revision = fx.commander.apply_calls[0]
        assert apply_revision == "disk-rev-1"
        assert candidate_id
        # 服务端确认后才推进 desired revision。
        assert fx.service.desired_revision == fx.commander.runtime_revision
        assert fx.service.desired_config_hash is not None
        # Commander disk 内容已被候选覆盖。
        assert (
            fx.commander.disk_files["cfg/tasks.yaml"] == (fx.config_dir / "tasks.yaml").read_bytes()
        )

    async def test_reload_propagates_restart_required(self, fx: _Fixture) -> None:
        fx.commander.restart_required = True
        _set_interval(fx.config_dir, 0.5)

        result = await fx.service.reload()

        assert result.success is True, result.errors
        assert result.restart_required is True

    async def test_unchanged_config_short_circuits_without_rpc(self, fx: _Fixture) -> None:
        first = await fx.service.reload()
        assert first.success is True, first.errors
        fx.commander.push_calls.clear()
        fx.commander.apply_calls.clear()
        fx.commander.snapshot_scopes.clear()

        second = await fx.service.reload()

        assert second.success is True, second.errors
        assert second.diff.has_changes is False
        assert fx.commander.push_calls == []
        assert fx.commander.apply_calls == []
        assert fx.commander.snapshot_scopes == []

    async def test_first_reload_without_changes_does_not_touch_commander(
        self, fx: _Fixture
    ) -> None:
        """磁盘配置与基线一致时，首次 reload 同样短路（幂等 bootstrap）。"""
        result = await fx.service.reload()

        assert result.success is True, result.errors
        assert fx.commander.push_calls == []
        assert fx.commander.apply_calls == []

    async def test_force_bypasses_short_circuit(self, fx: _Fixture) -> None:
        """force_workers 下即便无 diff 也要完成一次快照事务（允许空文件集）。"""
        result = await fx.service.reload(force_workers=True)

        assert result.success is True, result.errors
        assert len(fx.commander.push_calls) == 1
        assert len(fx.commander.apply_calls) == 1
        assert fx.commander.push_calls[0][0] == {}


# ---------------------------------------------------------------------------
# reload 失败路径：本地磁盘配置
# ---------------------------------------------------------------------------


class TestReloadInvalidDiskConfig:
    async def test_invalid_config_fails_closed_without_rpc(self, fx: _Fixture) -> None:
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
        assert result.errors[0].startswith("local configuration invalid:")
        # 正式协议下 Web 不接管磁盘内容：失败现场原样保留，交人工对账。
        assert (fx.config_dir / "tasks.yaml").read_bytes() != original_tasks
        assert ghost.exists()
        # 非法配置不得触及 Commander 任何快照 RPC。
        assert fx.commander.push_calls == []
        assert fx.commander.apply_calls == []
        assert fx.commander.snapshot_scopes == []
        assert fx.service.desired_revision is None

    async def test_legacy_file_outside_commander_sources_fails_closed(self, fx: _Fixture) -> None:
        """旧平铺配置产物不在 Commander config_sources 内：不得猜测路径推送。"""
        # 假 Commander 的 disk 快照不拥有 tasks.yaml——模拟新旧配置结构不兼容。
        del fx.commander.disk_files["cfg/tasks.yaml"]
        _set_interval(fx.config_dir, 0.9)

        result = await fx.service.reload()

        assert result.success is False
        assert any("not in Commander config_sources" in e for e in result.errors)
        assert fx.commander.push_calls == []
        assert fx.commander.apply_calls == []
        assert fx.service.desired_revision is None

    async def test_snapshot_deletion_is_rejected(self, tmp_path: Path) -> None:
        """快照协议不支持删除：构造后消失的托管文件必须 fail-closed。"""
        config_dir = _write_site(tmp_path / "cfg")
        extra = config_dir / "extra.yaml"
        extra.write_text("extra: true\n", encoding="utf-8")
        commander = _FakeCommander(config_dir)
        service = ConfigService(
            config_dir,
            _FakeDirectory({COLLECTOR_ID: _FakeCollector()}),
            commander,
            ConfigService.load_directory(config_dir),
        )
        extra.unlink()

        result = await service.reload(force_workers=True)

        assert result.success is False
        assert any("does not support deletion" in e for e in result.errors)
        assert commander.push_calls == []


# ---------------------------------------------------------------------------
# reload 失败路径：Push 阶段
# ---------------------------------------------------------------------------


class TestPushFailures:
    async def test_push_rejection_fails_without_apply(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.9)
        fx.commander.push_outcomes.append(
            {"valid": False, "errors": ["schema violation: bad interval"]}
        )

        result = await fx.service.reload()

        assert result.success is False
        assert any("schema violation: bad interval" in e for e in result.errors)
        assert len(fx.commander.push_calls) == 1
        assert fx.commander.apply_calls == []
        assert fx.service.desired_revision is None

    async def test_push_base_revision_mismatch_fails(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.9)
        # 候选回执声称的 base 与服务端刚读到的 disk revision 不一致。
        fx.commander.push_outcomes.append({"valid": True, "base_revision": "disk-rev-tampered"})

        result = await fx.service.reload()

        assert result.success is False
        assert any("base revision differs" in e for e in result.errors)
        assert fx.commander.apply_calls == []

    async def test_push_rpc_error_is_unknown_and_not_retried(self, fx: _Fixture) -> None:
        """Push RPC 异常 = 事务不确定：报告 not confirmed，绝不自动重试。"""
        _set_interval(fx.config_dir, 0.9)
        fx.commander.push_outcomes.append(TimeoutError("push response lost"))

        result = await fx.service.reload()

        assert result.success is False
        assert any("not confirmed" in e for e in result.errors)
        assert len(fx.commander.push_calls) == 1
        assert fx.commander.apply_calls == []

    async def test_empty_disk_revision_fails_before_push(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.9)
        fx.commander.disk_revision = ""

        result = await fx.service.reload()

        assert result.success is False
        assert any("no disk revision" in e for e in result.errors)
        assert fx.commander.push_calls == []


# ---------------------------------------------------------------------------
# reload 失败路径：Apply 阶段
# ---------------------------------------------------------------------------


class TestApplyFailures:
    async def test_apply_convergence_errors_fail_but_no_rollback_rpc(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.7)
        fx.commander.apply_outcomes.append({"errors": ["collector convergence failed"]})

        result = await fx.service.reload()

        assert result.success is False
        assert any("collector convergence failed" in e for e in result.errors)
        assert len(fx.commander.push_calls) == 1
        assert len(fx.commander.apply_calls) == 1
        # 没有 Abort/回滚接口：失败只报告，不自动发起第二个事务。
        assert fx.service.desired_revision is None

    async def test_apply_rpc_error_is_unknown_and_not_retried(self, fx: _Fixture) -> None:
        """Apply 超时后现场状态未知：禁止自动重试 Apply（可能已落盘）。"""
        _set_interval(fx.config_dir, 0.7)
        fx.commander.apply_outcomes.append(TimeoutError("apply response lost"))

        result = await fx.service.reload()

        assert result.success is False
        assert any("not confirmed" in e for e in result.errors)
        assert len(fx.commander.push_calls) == 1
        assert len(fx.commander.apply_calls) == 1
        assert fx.service.desired_revision is None

    async def test_apply_confirmation_mismatch_fails(self, fx: _Fixture) -> None:
        """Apply 回执的 file_commit 与回查 disk revision 不符：拒绝声称成功。"""
        _set_interval(fx.config_dir, 0.7)
        fx.commander.confirm_mismatch = True

        result = await fx.service.reload()

        assert result.success is False
        assert any("cannot be confirmed" in e for e in result.errors)
        assert fx.service.desired_revision is None


# ---------------------------------------------------------------------------
# 停机语义
# ---------------------------------------------------------------------------


class TestShutdownSemantics:
    async def test_reload_rejected_after_transactions_closed(self, fx: _Fixture) -> None:
        fx.service.stop_accepting_transactions()

        result = await fx.service.reload()

        assert result.success is False
        assert result.errors == ["config transactions are shutting down"]
        assert fx.commander.push_calls == []
        assert fx.commander.apply_calls == []

    async def test_wait_for_transactions_returns_true_when_idle(self, fx: _Fixture) -> None:
        assert await fx.service.wait_for_transactions(timeout=1.0) is True


# ---------------------------------------------------------------------------
# reconcile_workers（只读取证，绝不自动重新 Apply）
# ---------------------------------------------------------------------------


class TestReconcileWorkers:
    async def test_no_confirmed_revision_reported_for_commander(self, fx: _Fixture) -> None:
        outcomes = await fx.service.reconcile_workers()

        assert outcomes == {COMMANDER_WORKER_ID: "no-confirmed-remote-revision"}

    async def test_already_current_after_successful_reload(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.service.reload()).success is True
        fx.commander.push_calls.clear()
        fx.commander.apply_calls.clear()

        outcomes = await fx.service.reconcile_workers()

        assert outcomes == {COMMANDER_WORKER_ID: "already-current"}
        # 只读取证：reconcile 不发起任何事务 RPC。
        assert fx.commander.push_calls == []
        assert fx.commander.apply_calls == []

    async def test_drifted_commander_is_reported_not_auto_reconciled(self, fx: _Fixture) -> None:
        _set_interval(fx.config_dir, 0.6)
        assert (await fx.service.reload()).success is True
        # 模拟 Commander 偏离：runtime revision 落后（如重启丢了配置）。
        fx.commander.runtime_revision = "stale"
        fx.commander.push_calls.clear()

        outcomes = await fx.service.reconcile_workers()

        assert outcomes == {COMMANDER_WORKER_ID: "revision-mismatch"}
        assert fx.commander.push_calls == []

    async def test_not_running_reported(self, fx: _Fixture) -> None:
        fx.commander.running = False

        outcomes = await fx.service.reconcile_workers()

        assert outcomes == {COMMANDER_WORKER_ID: "not-running"}

    async def test_status_error_reported(self, fx: _Fixture) -> None:
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
    def test_bootstrap_sets_only_local_hash(self, fx: _Fixture) -> None:
        """启动基线仅代表本地配置，不得伪装成 Commander runtime revision。"""
        fx.service.initialize_desired_revision()

        assert fx.service.desired_revision is None
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

        def _add_device(data: dict[str, Any]) -> None:
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
    """并发 reload 由事务锁串行化：只有一次 Push/Apply 事务。

    第二个 reload 在锁内看到第一个已应用的结果并无变更短路——绝不出现
    两个事务交错或 Commander 收到重复候选的情况。
    """
    import asyncio

    _set_interval(fx.config_dir, 0.5)

    first, second = await asyncio.gather(fx.service.reload(), fx.service.reload())

    assert first.success and second.success
    assert len(fx.commander.push_calls) == 1
    assert len(fx.commander.apply_calls) == 1


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
        """无变更短路不经过 _commit：钩子不应触发。"""
        calls = 0

        def counting(previous: ConfigSnapshot, candidate: ConfigSnapshot) -> None:
            nonlocal calls
            calls += 1

        fx.service.add_commit_hook(counting)
        result = await fx.service.reload()

        assert result.success is True
        assert result.diff.has_changes is False
        assert calls == 0
