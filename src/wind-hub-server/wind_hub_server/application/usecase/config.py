"""Server 配置编排。

Server 负责配置文件加载、校验、diff 与成功基线；Collector 只执行 reload。
本模块不依赖 Collector Runtime，只通过 CollectorPort 通知独立 Collector。
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4
from pathlib import Path

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
        if old_devices[device_id].model_dump()
        != new_devices[device_id].model_dump()
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

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def current_config(self) -> Config:
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
        """基于当前稳定磁盘配置建立 bootstrap desired revision。

        若已有成功事务产生的 desired revision 则保持不变。初始化前重新加载并
        校验磁盘配置，确认其与 Server current_config 无结构差异且读取期间未变化。
        """
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
        diff = compute_diff(self._current, candidate)
        if diff.has_any_changes:
            raise ValueError(
                "current config differs from disk while initializing desired revision"
            )

        self._desired_revision = f"bootstrap-{config_hash}"
        self._desired_config_hash = config_hash

    async def reload(self, *, force_workers: bool = False) -> ReloadResult:
        """串行执行双 Worker 配置事务。

        所有入口（Web Apply、SIGHUP、回滚、恢复）最终都进入本方法，因此同一
        Server 进程内任一时刻只允许一个 revision 处于 prepare/activate 阶段。
        """
        async with self._transaction_lock:
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
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=[str(exc) or type(exc).__name__],
                duration_ms=(time.monotonic() - started) * 1000,
            )

        diff = compute_diff(self._current, candidate)
        if not diff.has_any_changes and not force_workers:
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

        for name, result in zip(
            ("collector", "commander"),
            prepared,
            strict=True,
        ):
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
                remote_errors = [
                    str(item) for item in list(result.get("errors") or [])
                ]
                errors.extend(
                    remote_errors or [f"{name} prepare failed"]
                )

        if not all(prepare_ok):
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

        for name, result in zip(
            ("collector", "commander"),
            activated,
            strict=True,
        ):
            if isinstance(result, BaseException):
                confirmed, confirm_error = await self._confirm_active_revision(
                    name,
                    revision_id,
                )
                activate_ok.append(confirmed)
                if not confirmed:
                    detail = str(result) or type(result).__name__
                    if confirm_error is None:
                        errors.append(
                            f"{name} activate failed after RPC error: {detail}"
                        )
                    else:
                        errors.append(
                            f"{name} activate outcome unknown after RPC error: "
                            f"{detail}; status check failed: {confirm_error}"
                        )
                continue

            success = bool(result.get("success"))
            activate_ok.append(success)
            if not success:
                remote_errors = [
                    str(item) for item in list(result.get("errors") or [])
                ]
                errors.extend(
                    remote_errors or [f"{name} activate failed"]
                )

        success = all(activate_ok)
        if success:
            self._current = candidate
            self._desired_revision = revision_id
            self._desired_config_hash = config_hash

        return ReloadResult(
            success=success,
            diff=diff,
            errors=errors,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    async def reconcile_workers(self) -> dict[str, str]:
        """把 active_revision 偏离 desired_revision 的 Worker 收敛到目标版本。

        本方法只处理已有 desired revision；不会自行生成新配置版本。若本地磁盘
        已偏离 desired_config_hash，Worker Prepare 会因 hash 不一致而拒绝，
        从而避免把未正式 Apply 的文件内容误激活。
        """
        async with self._transaction_lock:
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

            for name, status in zip(
                ("collector", "commander"),
                statuses,
                strict=True,
            ):
                if isinstance(status, BaseException):
                    outcomes[name] = (
                        "status-error:"
                        + (str(status) or type(status).__name__)
                    )
                    continue

                active_revision = str(status.get("active_revision") or "")
                if active_revision == revision_id:
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
        """对单个偏离 Worker 执行 Prepare/Activate 并确认最终 revision。"""
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
            return (
                "prepare-hash-mismatch:"
                f"{prepared.get('config_hash')!s}"
            )

        try:
            if worker == "collector":
                activated = await self._collector.activate_config(revision_id)
            else:
                activated = await self._commander.activate_config(revision_id)
        except Exception as exc:
            confirmed, confirm_error = await self._confirm_active_revision(
                worker,
                revision_id,
            )
            if confirmed:
                return "reconciled"
            if confirm_error is not None:
                return (
                    "activate-unknown:"
                    f"{str(exc) or type(exc).__name__}; "
                    f"status={confirm_error}"
                )
            return "activate-failed:" + (str(exc) or type(exc).__name__)

        if bool(activated.get("success")):
            return "reconciled"

        errors = [str(item) for item in list(activated.get("errors") or [])]
        return "activate-failed:" + ("; ".join(errors) or "unknown")

    async def _confirm_active_revision(
        self,
        worker: str,
        revision_id: str,
    ) -> tuple[bool, str | None]:
        """RPC 异常后回查 Worker active_revision，区分失败与结果未知。"""
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
        return active_revision == revision_id, None


__all__ = ["Config", "ConfigUseCase", "compute_diff"]
