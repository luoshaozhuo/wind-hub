"""Config use case——配置加载、校验、diff 与热重载的应用编排。

职责边界：本用例只做「load → validate → diff → runtime.reconfigure →
commit current config」的编排；具体的设备/sink 增删重建、Task Instance
重新展开与点表重注入全部由
:class:`~wind_hub.application.runtime.runtime.Runtime` 的
:meth:`Runtime.reconfigure` 执行——本用例不直接触碰任何运行时组件。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from wind_hub.application.runtime.collector_identity import fingerprint_config_set
from wind_hub.application.runtime.runtime import Runtime
from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config
from wind_hub.domain.model.reload import (
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
        # Runtime 队列/超时与进程级 ADS 本机身份不能在现有对象图上安全原地切换；
        # 显式进入 diff，由 reload 返回“需要重启”，禁止静默吞掉配置变化。
        runtime_changed=old.system.runtime != new.system.runtime,
        ads_changed=old.system.ads != new.system.ads,
    )


class ConfigUseCase:
    """配置热重载用例编排。

    ``reload()`` 编排热重载，``current_config`` 暴露当前配置。

    ``reload()`` 流程：

    1. 从磁盘加载新配置（load + schema 校验，失败即中止，不应用任何改动）；
    2. 计算 :class:`ConfigDiff`（无变更则直接返回成功）；
    3. 调用 :meth:`Runtime.reconfigure` 执行全部运行时重构；
    4. 提交新配置为当前快照，返回 :class:`ReloadResult`。

    reconfigure 返回的错误列表原样汇入 ``ReloadResult.errors``。任一运行时
    重构失败时不提交新快照；Runtime 的增量操作按当前状态收敛，因此下一次
    相同 reload 可安全重试未完成部分。
    """

    def __init__(self, config_dir: str | Path, runtime: Runtime, current_config: Config) -> None:
        self._config_dir = Path(config_dir)
        self._runtime = runtime
        self._config_hash = fingerprint_config_set(self._config_dir)

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

    async def reload(self) -> ReloadResult:
        """执行一次增量热重载。

        Returns:
            ReloadResult，包含 diff、错误列表和耗时。

        Notes:
            新 YAML 加载失败时不触碰 Runtime。Runtime.reconfigure 允许部分应用；
            若返回错误，成功应用的部分不回滚，但当前配置快照仍保持旧值，下一次
            相同 reload 会基于 Runtime 当前状态安全重试未完成部分。
        """
        t0 = time.monotonic()

        # 1. 先加载并完成 schema 校验；失败时不应用任何运行时变化。
        try:
            new_cfg = load_config(self._config_dir)
        except Exception as exc:
            logger.error("Reload aborted — config load failed: %s", exc)
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=[str(exc)],
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # 2. 计算纯结构 diff。
        diff = compute_diff(self._current, new_cfg)
        if not diff.has_any_changes:
            # site/interfaces 等 Collector 不消费的元数据即使变化，也同步当前快照，
            # 避免 current_config 与磁盘配置长期漂移。
            self._current = new_cfg
            self._config_hash = fingerprint_config_set(self._config_dir)
            logger.info("Reload: no runtime-affecting changes detected")
            return ReloadResult(
                success=True,
                diff=diff,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        restart_required: list[str] = []
        if diff.runtime_changed:
            restart_required.append("system.runtime")
        if diff.ads_changed:
            restart_required.append("system.ads")
        if restart_required:
            message = (
                "reload requires Collector restart for: "
                + ", ".join(restart_required)
            )
            logger.warning(message)
            return ReloadResult(
                success=False,
                diff=diff,
                errors=[message],
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # 3. 全部运行时重构交给 Runtime（设备/sink/task 增删重建、点表
        #    重注入的执行细节由 Runtime 负责，此处不直接调用任何组件操作）。
        errors = await self._runtime.reconfigure(new_cfg, diff)

        duration_ms = (time.monotonic() - t0) * 1000
        if errors:
            logger.warning(
                "Reload partially failed in %.1f ms; current config snapshot retained",
                duration_ms,
            )
            return ReloadResult(
                success=False,
                diff=diff,
                errors=errors,
                duration_ms=duration_ms,
            )

        # 4. 仅在全部运行时变更成功后提交新快照与指纹。
        self._current = new_cfg
        self._config_hash = fingerprint_config_set(self._config_dir)
        logger.info("Reload succeeded in %.1f ms", duration_ms)
        return ReloadResult(
            success=True,
            diff=diff,
            errors=[],
            duration_ms=duration_ms,
        )
