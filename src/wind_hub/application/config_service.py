"""Config service — 配置加载、校验、diff 与热重载用例编排。

职责边界：本服务只做「load → validate → diff → runtime.reconfigure →
commit current config」的编排；具体的设备/sink 增删重建、路由表与处理链
替换全部由 :class:`~wind_hub.application.runtime.runtime.Runtime` 的
:meth:`Runtime.reconfigure` 执行——本服务不直接触碰任何运行时组件。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from wind_hub.application.runtime.runtime import Runtime
from wind_hub.config.loader import load_config
from wind_hub.config.schema import Config
from wind_hub.domain.model.reload import ConfigDiff, DeviceDiff, ReloadResult, SinkDiff
from wind_hub.domain.port.inbound import ConfigUseCase

logger = logging.getLogger(__name__)


def compute_diff(old: Config, new: Config) -> ConfigDiff:
    """Compute the difference between two Config snapshots.

    Comparison rules:

    - **Devices**: keyed by ``device_id``.  A device is "updated" when
      any field of its ``DeviceConfig`` differs (deep equality via
      ``model_dump()``).
    - **Sinks**: keyed by ``sink.name``, same logic.
    - **Point tables**: keyed by table name — 新增/删除/内容变化的表名进入
      ``point_tables_changed``；任意表变化同时置 ``points_changed=True``。
    - **Rules**: list-level deep comparison — any difference sets
      ``rules_changed=True``.
    - **Pipeline**: processor list comparison.

    This is pure logic — no IO, no side-effects.
    """
    # -- devices -----------------------------------------------------------
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

    # -- sinks -------------------------------------------------------------
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

    # -- point tables / rules / pipeline -------------------------------------
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
    rules_changed = _items_changed(old.routing.rules, new.routing.rules)
    pipeline_changed = old.system.pipeline.processors != new.system.pipeline.processors

    return ConfigDiff(
        devices=devices,
        sinks=sinks,
        points_changed=bool(point_tables_changed),
        point_tables_changed=point_tables_changed,
        rules_changed=rules_changed,
        pipeline_changed=pipeline_changed,
    )


def _items_changed(old_items: Sequence[Any], new_items: Sequence[Any]) -> bool:
    """True when two lists of pydantic models differ."""
    if len(old_items) != len(new_items):
        return True
    return any(a.model_dump() != b.model_dump() for a, b in zip(old_items, new_items, strict=True))


class ConfigService(ConfigUseCase):
    """配置服务——热重载用例编排。

    实现 :class:`~wind_hub.domain.port.inbound.ConfigUseCase`：
    ``reload()`` 编排热重载，``current_config`` 暴露当前配置。

    ``reload()`` 流程：

    1. 从磁盘加载新配置（load + schema 校验，失败即中止，不应用任何改动）；
    2. 计算 :class:`ConfigDiff`（无变更则直接返回成功）；
    3. 调用 :meth:`Runtime.reconfigure` 执行全部运行时重构；
    4. 提交新配置为当前快照，返回 :class:`ReloadResult`。

    reconfigure 返回的错误列表原样汇入 ``ReloadResult.errors``——部分失败
    时 ``success`` 为 ``False``，但配置快照仍提交（与旧语义一致：已应用的
    变更不回滚，下一次 reload 以新快照为 diff 基准）。
    """

    def __init__(self, config_dir: str | Path, runtime: Runtime) -> None:
        self._config_dir = Path(config_dir)
        self._runtime = runtime

        # Load initial config
        self._current = load_config(self._config_dir)

    @property
    def current_config(self) -> Config:
        """The currently active configuration."""
        return self._current

    async def reload(self) -> ReloadResult:
        """Run a full hot-reload cycle.

        Returns a ``ReloadResult`` describing what changed and whether
        the operation succeeded.
        """
        t0 = time.monotonic()

        # 1. Load new config — bail early on failure
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

        # 2. Compute diff
        diff = compute_diff(self._current, new_cfg)
        if not diff.has_any_changes:
            logger.info("Reload: no changes detected")
            return ReloadResult(
                success=True,
                diff=diff,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # 3. 全部运行时重构交给 Runtime（设备/sink 增删重建、路由表与处理链
        #    替换的执行细节由 Runtime 负责，此处不直接调用任何组件操作）。
        errors = await self._runtime.reconfigure(new_cfg, diff)

        # 4. Commit new config
        self._current = new_cfg

        duration_ms = (time.monotonic() - t0) * 1000
        success = len(errors) == 0
        logger.info(
            "Reload %s in %.1f ms",
            "succeeded" if success else "partially failed",
            duration_ms,
        )
        return ReloadResult(
            success=success,
            diff=diff,
            errors=errors,
            duration_ms=duration_ms,
        )
