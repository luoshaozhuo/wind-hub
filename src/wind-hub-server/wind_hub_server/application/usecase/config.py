"""Server 配置编排。

Server 负责配置文件加载、校验、diff 与成功基线；Collector/Commander 只执行
Server 指定 revision/hash 的 Prepare/Activate/Abort。配置事务在进程内串行，
停机时可关闭新事务入口并等待当前事务完成。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from uuid import uuid4

from wind_hub_core.config.fingerprint import fingerprint_config_set
from wind_hub_core.config.loader import load_config
from wind_hub_core.config.schema import Config
from wind_hub_core.model.reload import (
    ConfigDiff,
    DeviceDiff,
    ReloadResult,
    SinkDiff,
    TaskDiff,
)
from wind_hub_server.application.port.worker import CollectorPort, CommanderPort


def compute_diff(old: Config, new: Config) -> ConfigDiff:
    """计算两个完整配置快照的结构化差异。"""
    old_devices = {item.device_id: item for item in old.devices.devices}
    new_devices = {item.device_id: item for item in new.devices.devices}
    old_ids, new_ids = set(old_devices), set(new_devices)
    devices = DeviceDiff(
        added=sorted(new_ids - old_ids),
        removed=sorted(old_ids - new_ids),
    )
    devices.updated = sorted(
        device_id
        for device_id in old_ids & new_ids
        if old_devices[device_id].model_dump() != new_devices[device_id].model_dump()
    )
    devices.unchanged = sorted((old_ids & new_ids) - set(devices.updated))

    old_sinks = {item.name: item for item in old.system.sinks}
    new_sinks = {item.name: item for item in new.system.sinks}
    old_sink_ids, new_sink_ids = set(old_sinks), set(new_sinks)
    sinks = SinkDiff(
        added=sorted(new_sink_ids - old_sink_ids),
        removed=sorted(old_sink_ids - new_sink_ids),
    )
    sinks.updated = sorted(
        name
        for name in old_sink_ids & new_sink_ids
        if old_sinks[name].model_dump() != new_sinks[name].model_dump()
    )
    sinks.unchanged = sorted((old_sink_ids & new_sink_ids) - set(sinks.updated))

    old_tasks = {item.task_id: item for item in old.tasks.tasks}
    new_tasks = {item.task_id: item for item in new.tasks.tasks}
    old_task_ids, new_task_ids = set(old_tasks), set(new_tasks)
    tasks = TaskDiff(
        added=sorted(new_task_ids - old_task_ids),
        removed=sorted(old_task_ids - new_task_ids),
    )
    tasks.updated = sorted(
        task_id
        for task_id in old_task_ids & new_task_ids
        if old_tasks[task_id].model_dump() != new_tasks[task_id].model_dump()
    )
    tasks.unchanged = sorted((old_task_ids & new_task_ids) - set(tasks.updated))

    old_tables = old.point_tables.tables
    new_tables = new.point_tables.tables
    names = set(old_tables) | set(new_tables)
    changed_tables = sorted(
        name
        for name in names
        if name not in old_tables
        or name not in new_tables
        or old_tables[name].model_dump() != new_tables[name].model_dump()
    )
    return ConfigDiff(
        devices=devices,
        sinks=sinks,
        tasks=tasks,
        points_changed=bool(changed_tables),
        point_tables_changed=changed_tables,
        units_changed=old.units != new.units,
    )


class ConfigUseCase:
    """Server 配置成功基线与双 Worker prepare/activate 编排。"""

    def __init__(
        self,
        config_dir: str | Path,
        collector: CollectorPort,
        commander: CommanderPort,
        current_config: Config,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._collector = collector
        self._commander = commander
        self._current = current_config
        self._desired_revision: str | None = None
        self._desired_config_hash: str | None = None
        self._transaction_lock = asyncio.Lock()
        self._accept_transactions = True
        self._applied_files = self._snapshot_config_files()
        self._applied_config_hash = fingerprint_config_set(self._config_dir)

    @property
    def config_dir(self) -> Path:
        """返回 Server 管理的配置目录。"""
        return self._config_dir

    @property
    def current_config(self) -> Config:
        """返回最近一次成功提交的 Server 配置基线。"""
        return self._current

    @property
    def desired_revision(self) -> str | None:
        """最近一次成功激活到全部 Worker 的目标 revision。"""
        return self._desired_revision

    @property
    def desired_config_hash(self) -> str | None:
        """desired_revision 对应的配置集指纹。"""
        return self._desired_config_hash

    @staticmethod
    def load_directory(config_dir: str | Path) -> Config:
        """完整加载并校验指定配置目录。"""
        return load_config(config_dir)

    def load_disk(self) -> Config:
        """加载当前 Server 管理的配置目录。"""
        return load_config(self._config_dir)

    def initialize_desired_revision(self) -> None:
        """基于当前稳定磁盘配置建立 bootstrap desired revision。"""
        if self._desired_revision is not None:
            return
        before_hash = fingerprint_config_set(self._config_dir)
        candidate = self.load_disk()
        config_hash = fingerprint_config_set(self._config_dir)
        if before_hash != config_hash:
            raise ValueError(
                "config changed while initializing desired revision: "
                f"before={before_hash} after={config_hash}"
            )
        if compute_diff(self._current, candidate).has_any_changes:
            raise ValueError(
                "current config differs from disk while initializing desired revision"
            )
        self._desired_revision = f"bootstrap-{config_hash}"
        self._desired_config_hash = config_hash

    def stop_accepting_transactions(self) -> None:
        """关闭新配置事务入口；已持锁事务继续运行到自然结束。"""
        self._accept_transactions = False

    async def wait_for_transactions(self, timeout: float) -> bool:
        """等待当前配置事务释放全局锁。

        Args:
            timeout: 最大等待秒数。

        Returns:
            在超时前进入并离开事务锁返回 True，否则返回 False。
        """
        self.stop_accepting_transactions()
        try:
            await asyncio.wait_for(self._wait_for_transaction_lock(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def _wait_for_transaction_lock(self) -> None:
        """等待事务锁空闲；不执行任何配置动作。"""
        async with self._transaction_lock:
            return

    def _closed_result(self) -> ReloadResult:
        """构造停机阶段拒绝新配置事务的稳定结果。"""
        return ReloadResult(
            success=False,
            diff=ConfigDiff(),
            errors=["config transactions are shutting down"],
        )

    async def reload(self, *, force_workers: bool = False) -> ReloadResult:
        """串行执行双 Worker 配置事务。"""
        if not self._accept_transactions:
            return self._closed_result()
        async with self._transaction_lock:
            if not self._accept_transactions:
                return self._closed_result()
            return await self._reload_locked(force_workers=force_workers)

    async def _reload_locked(self, *, force_workers: bool) -> ReloadResult:
        """在配置事务锁内执行一次完整 prepare/activate。"""
        started = time.monotonic()
        try:
            before_hash = fingerprint_config_set(self._config_dir)
            candidate = self.load_disk()
            config_hash = fingerprint_config_set(self._config_dir)
            if before_hash != config_hash:
                raise ValueError(
                    "config changed while loading candidate: "
                    f"before={before_hash} after={config_hash}"
                )
        except Exception as exc:
            errors = [str(exc) or type(exc).__name__]
            errors.extend(self._restore_applied_files())
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=errors,
                duration_ms=(time.monotonic() - started) * 1000,
            )

        diff = compute_diff(self._current, candidate)
        if (
            not diff.has_any_changes
            and not force_workers
            and config_hash == self._applied_config_hash
        ):
            return ReloadResult(
                success=True,
                diff=diff,
                duration_ms=(time.monotonic() - started) * 1000,
            )

        revision_id = uuid4().hex
        prepared = await asyncio.gather(
            self._collector.prepare_config(
                revision_id,
                config_hash,
                force_reconfigure=force_workers,
            ),
            self._commander.prepare_config(revision_id, config_hash),
            return_exceptions=True,
        )
        errors: list[str] = []
        prepare_ok: list[bool] = []
        for name, result in zip(("collector", "commander"), prepared, strict=True):
            if isinstance(result, BaseException):
                errors.append(
                    f"{name} prepare failed: {str(result) or type(result).__name__}"
                )
                prepare_ok.append(False)
                continue
            success = bool(result.get("success"))
            remote_hash = str(result.get("config_hash") or "")
            if success and remote_hash != config_hash:
                success = False
                errors.append(
                    f"{name} prepare hash mismatch: "
                    f"expected={config_hash} actual={remote_hash}"
                )
            prepare_ok.append(success)
            if not success:
                remote_errors = [str(item) for item in list(result.get("errors") or [])]
                errors.extend(remote_errors or [f"{name} prepare failed"])

        if not all(prepare_ok):
            errors.extend(await self._abort_prepared_revision(revision_id))
            errors.extend(self._restore_applied_files())
            return ReloadResult(
                success=False,
                diff=diff,
                errors=errors,
                duration_ms=(time.monotonic() - started) * 1000,
            )

        activated = await asyncio.gather(
            self._collector.activate_config(revision_id),
            self._commander.activate_config(revision_id),
            return_exceptions=True,
        )
        activate_ok: list[bool] = []
        for name, result in zip(("collector", "commander"), activated, strict=True):
            if isinstance(result, BaseException):
                confirmed, confirm_error = await self._confirm_active_config(
                    name,
                    revision_id,
                    config_hash,
                )
                activate_ok.append(confirmed)
                if not confirmed:
                    detail = str(result) or type(result).__name__
                    if confirm_error is None:
                        errors.append(f"{name} activate failed after RPC error: {detail}")
                    else:
                        errors.append(
                            f"{name} activate outcome unknown after RPC error: "
                            f"{detail}; status check failed: {confirm_error}"
                        )
                continue

            success = bool(result.get("success"))
            remote_hash = str(result.get("active_config_hash") or "")
            if success and remote_hash and remote_hash != config_hash:
                success = False
                errors.append(
                    f"{name} activate hash mismatch: "
                    f"expected={config_hash} actual={remote_hash}"
                )
            activate_ok.append(success)
            if not success:
                remote_errors = [str(item) for item in list(result.get("errors") or [])]
                errors.extend(remote_errors or [f"{name} activate failed"])

        success = all(activate_ok)
        if success:
            self._current = candidate
            self._desired_revision = revision_id
            self._desired_config_hash = config_hash
            self._applied_files = self._snapshot_config_files()
            self._applied_config_hash = config_hash
        else:
            errors.extend(await self._abort_prepared_revision(revision_id))
            errors.extend(await self._rollback_to_applied_config())
        return ReloadResult(
            success=success,
            diff=diff,
            errors=errors,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _config_roots(self) -> list[Path]:
        """返回参与配置指纹的 YAML 根目录。"""
        site_dir = self._config_dir.resolve()
        roots = [site_dir]
        common_dir = site_dir.parent / "common"
        if common_dir.is_dir() and common_dir != site_dir:
            roots.append(common_dir)
        return roots

    def _snapshot_config_files(self) -> dict[Path, bytes]:
        """保存当前已接受配置集的 YAML 文件快照。"""
        snapshot: dict[Path, bytes] = {}
        for root in self._config_roots():
            for path in root.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in {".yaml", ".yml"}
                    and ".history" not in path.relative_to(root).parts
                ):
                    snapshot[path.resolve()] = path.read_bytes()
        return snapshot

    def _restore_applied_files(self) -> list[str]:
        """把磁盘 YAML 恢复到最近一次成功激活的配置集。"""
        errors: list[str] = []
        try:
            current = {
                path.resolve()
                for root in self._config_roots()
                for path in root.rglob("*")
                if (
                    path.is_file()
                    and path.suffix.lower() in {".yaml", ".yml"}
                    and ".history" not in path.relative_to(root).parts
                )
            }
            for path in current - set(self._applied_files):
                path.unlink()
            for path, content in self._applied_files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(f".{path.name}.rollback.tmp")
                temp.write_bytes(content)
                os.replace(temp, path)
        except Exception as exc:
            errors.append(
                "restore applied config files failed: "
                + (str(exc) or type(exc).__name__)
            )
        return errors

    async def _rollback_to_applied_config(self) -> list[str]:
        """恢复磁盘并把全部 Worker 强制收敛到最近成功配置。"""
        errors = self._restore_applied_files()
        if errors:
            return errors

        revision_id = self._desired_revision or f"rollback-{self._applied_config_hash}"
        outcomes = await asyncio.gather(
            self._reconcile_worker(
                "collector",
                revision_id,
                self._applied_config_hash,
            ),
            self._reconcile_worker(
                "commander",
                revision_id,
                self._applied_config_hash,
            ),
        )
        for name, outcome in zip(("collector", "commander"), outcomes, strict=True):
            if outcome not in {"reconciled", "already-current"}:
                errors.append(f"{name} rollback failed: {outcome}")
        return errors

    async def _abort_prepared_revision(self, revision_id: str) -> list[str]:
        """Prepare 整体失败后尽力撤销两端同 revision 候选状态。"""
        results = await asyncio.gather(
            self._collector.abort_config(revision_id),
            self._commander.abort_config(revision_id),
            return_exceptions=True,
        )
        errors: list[str] = []
        for name, result in zip(("collector", "commander"), results, strict=True):
            if isinstance(result, BaseException):
                errors.append(
                    f"{name} abort failed: {str(result) or type(result).__name__}"
                )
                continue
            if not bool(result.get("success", False)):
                remote_errors = [str(item) for item in list(result.get("errors") or [])]
                errors.extend(remote_errors or [f"{name} abort failed"])
        return errors

    async def reconcile_workers(self) -> dict[str, str]:
        """按 revision + config hash 把 Worker 收敛到 desired 配置。"""
        if not self._accept_transactions:
            return {"collector": "transactions-closed", "commander": "transactions-closed"}
        async with self._transaction_lock:
            if not self._accept_transactions:
                return {"collector": "transactions-closed", "commander": "transactions-closed"}
            revision_id = self._desired_revision
            config_hash = self._desired_config_hash
            if revision_id is None or config_hash is None:
                return {
                    "collector": "no-desired-revision",
                    "commander": "no-desired-revision",
                }

            statuses = await asyncio.gather(
                self._collector.config_status(),
                self._commander.status(),
                return_exceptions=True,
            )
            outcomes: dict[str, str] = {}
            for name, status in zip(("collector", "commander"), statuses, strict=True):
                if isinstance(status, BaseException):
                    outcomes[name] = "status-error:" + (str(status) or type(status).__name__)
                    continue
                active_revision = str(status.get("active_revision") or "")
                active_hash = str(
                    status.get("active_config_hash")
                    or status.get("config_hash")
                    or ""
                )
                if active_revision == revision_id and active_hash == config_hash:
                    outcomes[name] = "already-current"
                    continue
                outcomes[name] = await self._reconcile_worker(
                    name,
                    revision_id,
                    config_hash,
                )
            return outcomes

    async def _reconcile_worker(
        self,
        worker: str,
        revision_id: str,
        config_hash: str,
    ) -> str:
        """对单个偏离 Worker 执行 Prepare/Activate 并确认最终配置状态。"""
        try:
            if worker == "collector":
                prepared = await self._collector.prepare_config(
                    revision_id,
                    config_hash,
                    force_reconfigure=True,
                )
            elif worker == "commander":
                prepared = await self._commander.prepare_config(
                    revision_id,
                    config_hash,
                )
            else:
                raise ValueError(f"unknown worker: {worker}")
        except Exception as exc:
            return "prepare-error:" + (str(exc) or type(exc).__name__)

        if not bool(prepared.get("success")):
            errors = [str(item) for item in list(prepared.get("errors") or [])]
            return "prepare-failed:" + ("; ".join(errors) or "unknown")
        if str(prepared.get("config_hash") or "") != config_hash:
            return "prepare-hash-mismatch:" + str(prepared.get("config_hash") or "")

        try:
            if worker == "collector":
                activated = await self._collector.activate_config(revision_id)
            else:
                activated = await self._commander.activate_config(revision_id)
        except Exception as exc:
            confirmed, confirm_error = await self._confirm_active_config(
                worker,
                revision_id,
                config_hash,
            )
            if confirmed:
                return "reconciled"
            if confirm_error is not None:
                return (
                    "activate-unknown:"
                    f"{str(exc) or type(exc).__name__}; status={confirm_error}"
                )
            return "activate-failed:" + (str(exc) or type(exc).__name__)

        if bool(activated.get("success")):
            active_hash = str(activated.get("active_config_hash") or "")
            if not active_hash or active_hash == config_hash:
                return "reconciled"
            return f"activate-hash-mismatch:{active_hash}"

        errors = [str(item) for item in list(activated.get("errors") or [])]
        return "activate-failed:" + ("; ".join(errors) or "unknown")

    async def _confirm_active_config(
        self,
        worker: str,
        revision_id: str,
        config_hash: str,
    ) -> tuple[bool, str | None]:
        """RPC 异常后回查 active revision/hash，区分失败与结果未知。"""
        try:
            if worker == "collector":
                status = await self._collector.config_status()
            elif worker == "commander":
                status = await self._commander.status()
            else:
                raise ValueError(f"unknown worker: {worker}")
        except Exception as exc:
            return False, str(exc) or type(exc).__name__

        active_revision = str(status.get("active_revision") or "")
        active_hash = str(
            status.get("active_config_hash")
            or status.get("config_hash")
            or ""
        )
        return active_revision == revision_id and active_hash == config_hash, None


__all__ = ["Config", "ConfigUseCase", "compute_diff"]
