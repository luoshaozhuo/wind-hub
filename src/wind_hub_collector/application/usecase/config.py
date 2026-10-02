"""Config use case——配置加载、校验、diff 与热重载的应用编排。

职责边界：本用例只做「load → validate → diff → runtime.reconfigure →
commit current config」的编排；具体的设备/sink 增删重建、Task Instance
重新展开与点表重注入全部由
:class:`~wind_hub_collector.application.runtime.runtime.Runtime` 的
:meth:`Runtime.reconfigure` 执行——本用例不直接触碰任何运行时组件。
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4
from pathlib import Path

from wind_hub_collector.application.runtime.collector_identity import fingerprint_config_set
from wind_hub_collector.application.runtime.runtime import Runtime
from wind_hub_core.config.loader import load_config
from wind_hub_core.config.schema import Config
from wind_hub_core.model.reload import (
    ConfigDiff,
    DeviceDiff,
    ReloadResult,
    SinkDiff,
    TaskDiff,
)

logger = logging.getLogger(__name__)


def compute_diff(old: Config, new: Config) -> ConfigDiff:
    """计算两个 Config 快照的结构化差异。

    Args:
        old: 当前已提交配置。
        new: 新加载并通过 schema 校验的配置。

    Returns:
        ConfigDiff。Device/Sink/Task 按稳定 ID 比较；point table 按表名和完整模型
        比较；units 只标记元数据变化。

    Notes:
        纯函数，不执行 I/O，也不修改 Runtime。
    """
    # Device diff。
    old_dev_ids = {d.device_id: d for d in old.devices.devices}
    new_dev_ids = {d.device_id: d for d in new.devices.devices}

    old_set = set(old_dev_ids)
    new_set = set(new_dev_ids)

    devices = DeviceDiff(
        added=sorted(new_set - old_set),
        removed=sorted(old_set - new_set),
    )

    updated: list[str] = []
    unchanged: list[str] = []
    for did in old_set & new_set:
        if old_dev_ids[did].model_dump() != new_dev_ids[did].model_dump():
            updated.append(did)
        else:
            unchanged.append(did)
    devices.updated = sorted(updated)
    devices.unchanged = sorted(unchanged)

    # Sink diff。
    old_sinks = {s.name: s for s in old.system.sinks}
    new_sinks = {s.name: s for s in new.system.sinks}

    old_sink_set = set(old_sinks)
    new_sink_set = set(new_sinks)

    sinks = SinkDiff(
        added=sorted(new_sink_set - old_sink_set),
        removed=sorted(old_sink_set - new_sink_set),
    )

    sink_updated: list[str] = []
    sink_unchanged: list[str] = []
    for name in old_sink_set & new_sink_set:
        if old_sinks[name].model_dump() != new_sinks[name].model_dump():
            sink_updated.append(name)
        else:
            sink_unchanged.append(name)
    sinks.updated = sorted(sink_updated)
    sinks.unchanged = sorted(sink_unchanged)

    # Task Definition diff。
    old_tasks = {t.task_id: t for t in old.tasks.tasks}
    new_tasks = {t.task_id: t for t in new.tasks.tasks}

    old_task_set = set(old_tasks)
    new_task_set = set(new_tasks)

    tasks = TaskDiff(
        added=sorted(new_task_set - old_task_set),
        removed=sorted(old_task_set - new_task_set),
    )

    task_updated: list[str] = []
    task_unchanged: list[str] = []
    for tid in old_task_set & new_task_set:
        if old_tasks[tid].model_dump() != new_tasks[tid].model_dump():
            task_updated.append(tid)
        else:
            task_unchanged.append(tid)
    tasks.updated = sorted(task_updated)
    tasks.unchanged = sorted(task_unchanged)

    # Point table diff。
    old_tables = old.point_tables.tables
    new_tables = new.point_tables.tables
    table_names = set(old_tables) | set(new_tables)
    point_tables_changed = sorted(
        name
        for name in table_names
        if name not in old_tables
        or name not in new_tables
        or old_tables[name].model_dump() != new_tables[name].model_dump()
    )
    return ConfigDiff(
        devices=devices,
        sinks=sinks,
        tasks=tasks,
        points_changed=bool(point_tables_changed),
        point_tables_changed=point_tables_changed,
        # units 是纯展示元数据：变化不触发设备重连，但要让新快照提交
        units_changed=old.units != new.units,
    )


class ConfigUseCase:
    """配置热重载用例编排。

    ``reload()`` 编排热重载，``current_config`` 暴露当前配置。

    ``reload()`` 流程：

    1. 从磁盘加载新配置（load + schema 校验，失败即中止，不应用任何改动）；
    2. 计算 :class:`ConfigDiff`（无变更则直接返回成功）；
    3. 调用 :meth:`Runtime.reconfigure` 执行全部运行时重构；
    4. 仅当全部运行时重构成功时，提交新配置为当前快照。

    reconfigure 返回的错误列表原样汇入 ``ReloadResult.errors``。部分失败时
    ``success=False`` 且成功基线保持不变；下一次 reload 会重新计算同一 diff
    并重试。Runtime 的重构路径必须保持可重复调用。
    """

    def __init__(self, config_dir: str | Path, runtime: Runtime, current_config: Config) -> None:
        self._config_dir = Path(config_dir)
        self._runtime = runtime
        self._config_hash = fingerprint_config_set(self._config_dir)
        self._active_revision = "startup"
        self._prepared_revision: str | None = None
        self._prepared_config: Config | None = None
        self._prepared_diff: ConfigDiff | None = None
        self._prepared_hash: str | None = None
        self._reload_lock = asyncio.Lock()

        # 初始快照必须由组合根注入（assemble 启动阶段的唯一一次
        # load_config 结果）——本类不自行加载，避免启动配置被重复加载、
        # 以及两次加载之间文件变化导致 Runtime 实际配置与 diff 基线不一致。
        self._current = current_config

    @property
    def config_dir(self) -> Path:
        """当前运行实例的配置目录。"""
        return self._config_dir

    @property
    def current_config(self) -> Config:
        """返回当前已提交、作为下次 diff 基线的配置快照。"""
        return self._current

    @property
    def config_hash(self) -> str:
        """当前已提交配置快照对应的 YAML 指纹。"""
        return self._config_hash

    @property
    def active_revision(self) -> str:
        """返回当前已激活配置版本。"""
        return self._active_revision

    @property
    def prepared_revision(self) -> str | None:
        """返回当前已准备但尚未激活的配置版本。"""
        return self._prepared_revision

    @property
    def prepared_hash(self) -> str | None:
        """返回当前已准备配置的指纹。"""
        return self._prepared_hash

    async def prepare_config(
        self,
        revision_id: str,
        expected_config_hash: str | None = None,
        *,
        force_reconfigure: bool = False,
    ) -> ReloadResult:
        """加载并校验候选配置，校验指纹后保存候选快照。"""
        started = time.monotonic()
        if not revision_id:
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=["revision_id must not be empty"],
                duration_ms=(time.monotonic() - started) * 1000,
            )

        try:
            before_hash = fingerprint_config_set(self._config_dir)
            candidate = load_config(self._config_dir)
            candidate_hash = fingerprint_config_set(self._config_dir)
            if before_hash != candidate_hash:
                raise ValueError(
                    "config changed while preparing: "
                    f"before={before_hash} after={candidate_hash}"
                )
            if (
                expected_config_hash is not None
                and candidate_hash != expected_config_hash
            ):
                raise ValueError(
                    "config hash mismatch: "
                    f"expected={expected_config_hash} actual={candidate_hash}"
                )
        except Exception as exc:
            logger.error("Prepare aborted — config load failed: %s", exc)
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=[str(exc) or type(exc).__name__],
                duration_ms=(time.monotonic() - started) * 1000,
            )

        async with self._reload_lock:
            diff = (
                self._runtime.convergence_diff(candidate)
                if force_reconfigure
                else compute_diff(self._current, candidate)
            )
            self._prepared_revision = revision_id
            self._prepared_config = candidate
            self._prepared_diff = diff
            self._prepared_hash = candidate_hash

        logger.info(
            "Prepared config revision=%s changed=%s force_reconfigure=%s",
            revision_id,
            diff.has_any_changes,
            force_reconfigure,
        )
        return ReloadResult(
            success=True,
            diff=diff,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    async def activate_config(self, revision_id: str) -> ReloadResult:
        """激活已准备配置；仅此阶段执行 Runtime.reconfigure。"""
        started = time.monotonic()

        async with self._reload_lock:
            if self._prepared_revision != revision_id:
                return ReloadResult(
                    success=False,
                    diff=ConfigDiff(),
                    errors=[
                        "prepared revision mismatch: "
                        f"expected={self._prepared_revision!r} requested={revision_id!r}"
                    ],
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            candidate = self._prepared_config
            diff = self._prepared_diff
            candidate_hash = self._prepared_hash

            if candidate is None or diff is None or candidate_hash is None:
                return ReloadResult(
                    success=False,
                    diff=ConfigDiff(),
                    errors=["no prepared configuration"],
                    duration_ms=(time.monotonic() - started) * 1000,
                )

            if diff.has_any_changes:
                errors = await self._runtime.reconfigure(candidate, diff)
                if errors:
                    return ReloadResult(
                        success=False,
                        diff=diff,
                        errors=errors,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )

            self._current = candidate
            self._config_hash = candidate_hash
            self._active_revision = revision_id
            self._prepared_revision = None
            self._prepared_config = None
            self._prepared_diff = None
            self._prepared_hash = None

        logger.info("Activated config revision=%s", revision_id)
        return ReloadResult(
            success=True,
            diff=diff,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    async def abort_config(self, revision_id: str) -> bool:
        """幂等清理指定 prepared revision，不修改当前 Runtime。"""
        async with self._reload_lock:
            if self._prepared_revision != revision_id:
                return False
            self._prepared_revision = None
            self._prepared_config = None
            self._prepared_diff = None
            self._prepared_hash = None
            return True

    async def reload(self) -> ReloadResult:
        """兼容旧调用：按 prepare → activate 完成一次增量热重载。"""
        revision_id = uuid4().hex
        prepared = await self.prepare_config(revision_id)
        if not prepared.success:
            return prepared
        return await self.activate_config(revision_id)
