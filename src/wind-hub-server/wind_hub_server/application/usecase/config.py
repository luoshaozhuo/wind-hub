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
        self._transaction_lock = asyncio.Lock()

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def current_config(self) -> Config:
        return self._current

    @staticmethod
    def load_directory(config_dir: str | Path) -> Config:
        """完整加载并校验指定配置目录。"""
        return load_config(config_dir)

    def load_disk(self) -> Config:
        """加载当前 Server 管理的配置目录。"""
        return load_config(self._config_dir)

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
            self._collector.prepare_config(revision_id, config_hash),
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

        return ReloadResult(
            success=success,
            diff=diff,
            errors=errors,
            duration_ms=(time.monotonic() - started) * 1000,
        )

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
