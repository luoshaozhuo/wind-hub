"""Server 配置编排。

Server 负责配置文件加载、校验、diff 与成功基线；Collector 只执行 reload。
本模块不依赖 Collector Runtime，只通过 CollectorPort 通知独立 Collector。
"""

from __future__ import annotations

import time
from pathlib import Path

from wind_hub_core.config.loader import load_config
from wind_hub_core.config.schema import Config
from wind_hub_core.model.reload import (
    ConfigDiff,
    DeviceDiff,
    ReloadResult,
    SinkDiff,
    TaskDiff,
)
from wind_hub_server.application.port.worker import CollectorPort


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
    """Server 配置成功基线与 Collector reload 编排。"""

    def __init__(
        self,
        config_dir: str | Path,
        collector: CollectorPort,
        current_config: Config,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._collector = collector
        self._current = current_config

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

    async def reload(self) -> ReloadResult:
        """校验本地候选配置并通知 Collector reload。"""
        started = time.monotonic()
        try:
            candidate = self.load_disk()
        except Exception as exc:
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=[str(exc) or type(exc).__name__],
                duration_ms=(time.monotonic() - started) * 1000,
            )

        diff = compute_diff(self._current, candidate)
        if not diff.has_any_changes:
            return ReloadResult(
                success=True,
                diff=diff,
                duration_ms=(time.monotonic() - started) * 1000,
            )

        try:
            remote = await self._collector.reload_config()
        except Exception as exc:
            return ReloadResult(
                success=False,
                diff=diff,
                errors=[str(exc) or type(exc).__name__],
                duration_ms=(time.monotonic() - started) * 1000,
            )

        success = bool(remote.get("success"))
        errors = [str(item) for item in list(remote.get("errors") or [])]
        if success:
            self._current = candidate

        return ReloadResult(
            success=success,
            diff=diff,
            errors=errors,
            duration_ms=(time.monotonic() - started) * 1000,
        )


__all__ = ["Config", "ConfigUseCase", "compute_diff"]
