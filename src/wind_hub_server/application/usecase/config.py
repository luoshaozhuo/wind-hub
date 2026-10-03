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

from wind_hub_core.config.diff import compute_diff
from wind_hub_core.config.fingerprint import fingerprint_config_set
from wind_hub_core.config.loader import load_config
from wind_hub_core.config.schema import Config
from wind_hub_core.model.reload import ConfigDiff, ReloadResult
from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.port.worker import CollectorPort, CommanderPort
from wind_hub_server.application.worker_model import COMMANDER_WORKER_ID


def _remote_errors(payload: dict[str, object]) -> list[str]:
    """提取参与者响应中的 errors 字段（缺失或非列表时返回空列表）。"""
    raw = payload.get("errors")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


class ConfigUseCase:
    """Server 配置成功基线与多 Worker prepare/activate 编排。"""

    def __init__(
        self,
        config_dir: str | Path,
        collectors: CollectorDirectory,
        commander: CommanderPort,
        current_config: Config,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._collectors = collectors
        self._commander = commander
        collector_ids = self._collectors.list_worker_ids()
        if not collector_ids:
            raise ValueError("at least one collector worker is required")
        if COMMANDER_WORKER_ID in collector_ids:
            raise ValueError(
                f"collector worker_id '{COMMANDER_WORKER_ID}' conflicts with commander"
            )
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
        """串行执行全部已登记 Worker 的配置事务。"""
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
            load_errors = [str(exc) or type(exc).__name__]
            load_errors.extend(self._restore_applied_files())
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=load_errors,
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
        participant_ids = self._participant_ids()
        prepared = await asyncio.gather(
            *(
                self._prepare_participant(
                    participant_id,
                    revision_id,
                    config_hash,
                    force_workers=force_workers,
                )
                for participant_id in participant_ids
            ),
            return_exceptions=True,
        )
        errors: list[str] = []
        prepare_ok: list[bool] = []
        for name, result in zip(participant_ids, prepared, strict=True):
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
                remote_errors = _remote_errors(result)
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
            *(
                self._activate_participant(participant_id, revision_id)
                for participant_id in participant_ids
            ),
            return_exceptions=True,
        )
        activate_ok: list[bool] = []
        for name, result in zip(participant_ids, activated, strict=True):
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
                remote_errors = _remote_errors(result)
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

    def _snapshot_config_files(self) -> dict[Path, bytes]:
        """保存当前已接受配置集的 YAML 文件快照。"""
        snapshot: dict[Path, bytes] = {}
        root = self._config_dir.resolve()
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
        participant_ids = self._participant_ids()
        outcomes = await asyncio.gather(
            *(
                self._reconcile_worker(
                    participant_id,
                    revision_id,
                    self._applied_config_hash,
                )
                for participant_id in participant_ids
            )
        )
        for name, outcome in zip(participant_ids, outcomes, strict=True):
            if outcome not in {"reconciled", "already-current"}:
                errors.append(f"{name} rollback failed: {outcome}")
        return errors

    async def _abort_prepared_revision(self, revision_id: str) -> list[str]:
        """Prepare/Activate 失败后尽力撤销全部参与者同 revision 候选状态。"""
        participant_ids = self._participant_ids()
        results = await asyncio.gather(
            *(
                self._abort_participant(participant_id, revision_id)
                for participant_id in participant_ids
            ),
            return_exceptions=True,
        )
        errors: list[str] = []
        for name, result in zip(participant_ids, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(
                    f"{name} abort failed: {str(result) or type(result).__name__}"
                )
                continue
            if not bool(result.get("success", False)):
                remote_errors = _remote_errors(result)
                errors.extend(remote_errors or [f"{name} abort failed"])
        return errors

    async def reconcile_workers(self) -> dict[str, str]:
        """按 revision + config hash 把全部 Worker 收敛到 desired 配置。"""
        participant_ids = self._participant_ids()
        if not self._accept_transactions:
            return {
                participant_id: "transactions-closed"
                for participant_id in participant_ids
            }
        async with self._transaction_lock:
            if not self._accept_transactions:
                return {
                    participant_id: "transactions-closed"
                    for participant_id in participant_ids
                }
            revision_id = self._desired_revision
            config_hash = self._desired_config_hash
            if revision_id is None or config_hash is None:
                return {
                    participant_id: "no-desired-revision"
                    for participant_id in participant_ids
                }

            statuses = await asyncio.gather(
                *(
                    self._status_participant(participant_id)
                    for participant_id in participant_ids
                ),
                return_exceptions=True,
            )
            outcomes: dict[str, str] = {}
            for name, status in zip(participant_ids, statuses, strict=True):
                if isinstance(status, BaseException):
                    outcomes[name] = (
                        "status-error:" + (str(status) or type(status).__name__)
                    )
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
            prepared = await self._prepare_participant(
                worker,
                revision_id,
                config_hash,
                force_workers=True,
            )
        except Exception as exc:
            return "prepare-error:" + (str(exc) or type(exc).__name__)

        if not bool(prepared.get("success")):
            errors = _remote_errors(prepared)
            return "prepare-failed:" + ("; ".join(errors) or "unknown")
        if str(prepared.get("config_hash") or "") != config_hash:
            return "prepare-hash-mismatch:" + str(prepared.get("config_hash") or "")

        try:
            activated = await self._activate_participant(worker, revision_id)
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

        errors = _remote_errors(activated)
        return "activate-failed:" + ("; ".join(errors) or "unknown")

    async def _confirm_active_config(
        self,
        worker: str,
        revision_id: str,
        config_hash: str,
    ) -> tuple[bool, str | None]:
        """RPC 异常后回查 active revision/hash，区分失败与结果未知。"""
        try:
            status = await self._status_participant(worker)
        except Exception as exc:
            return False, str(exc) or type(exc).__name__

        active_revision = str(status.get("active_revision") or "")
        active_hash = str(
            status.get("active_config_hash")
            or status.get("config_hash")
            or ""
        )
        return active_revision == revision_id and active_hash == config_hash, None

    def _participant_ids(self) -> list[str]:
        """返回稳定排序的配置事务参与者 ID。"""
        collector_ids = self._collectors.list_worker_ids()
        if COMMANDER_WORKER_ID in collector_ids:
            raise ValueError(
                f"collector worker_id '{COMMANDER_WORKER_ID}' conflicts with commander"
            )
        return [*collector_ids, COMMANDER_WORKER_ID]

    async def _collector_participant(self, worker: str) -> CollectorPort:
        """返回身份与逻辑 worker_id 一致的 Collector。"""
        collector = self._collectors.get(worker)
        status = await collector.config_status()
        reported_id = str(status.get("collector_id") or "")
        if reported_id != worker:
            raise RuntimeError(
                f"collector identity mismatch: expected={worker} "
                f"reported={reported_id or '<empty>'}"
            )
        return collector

    async def _prepare_participant(
        self,
        worker: str,
        revision_id: str,
        config_hash: str,
        *,
        force_workers: bool,
    ) -> dict[str, object]:
        """Prepare 单个配置事务参与者。"""
        if worker == COMMANDER_WORKER_ID:
            return await self._commander.prepare_config(revision_id, config_hash)
        collector = await self._collector_participant(worker)
        return await collector.prepare_config(
            revision_id,
            config_hash,
            force_reconfigure=force_workers,
        )

    async def _activate_participant(
        self,
        worker: str,
        revision_id: str,
    ) -> dict[str, object]:
        """Activate 单个配置事务参与者。"""
        if worker == COMMANDER_WORKER_ID:
            return await self._commander.activate_config(revision_id)
        collector = await self._collector_participant(worker)
        return await collector.activate_config(revision_id)

    async def _abort_participant(
        self,
        worker: str,
        revision_id: str,
    ) -> dict[str, object]:
        """Abort 单个配置事务参与者。"""
        if worker == COMMANDER_WORKER_ID:
            return await self._commander.abort_config(revision_id)
        collector = await self._collector_participant(worker)
        return await collector.abort_config(revision_id)

    async def _status_participant(self, worker: str) -> dict[str, object]:
        """读取单个配置事务参与者当前配置状态。"""
        if worker == COMMANDER_WORKER_ID:
            return await self._commander.status()
        collector = await self._collector_participant(worker)
        return await collector.config_status()


__all__ = ["Config", "ConfigUseCase", "compute_diff"]
