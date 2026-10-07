"""配置 diff 与热重载结果模型（Application 层）。

compute_diff 比较两个 :class:`CollectorConfig` 快照，产出结构化差异，
驱动 CollectorRuntime.reconfigure 的最小化重构。与旧实现的关键差异：
设备「updated」判定不仅比较 Device 聚合本身，还比较其协议参数与点表
绑定——这些在新架构中分别存于 core 快照的 device_options 与
device_model 绑定关系。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from .config import CollectorConfig


@dataclass(slots=True)
class DeviceDiff:
    """新旧配置间的 Device 差异。"""

    added: list[str] = field(default_factory=list)
    """新配置新增的 device_id。"""

    removed: list[str] = field(default_factory=list)
    """新配置删除的 device_id。"""

    updated: list[str] = field(default_factory=list)
    """两边都存在但设备聚合/协议参数/点表绑定发生变化的 device_id。"""

    unchanged: list[str] = field(default_factory=list)
    """配置完全一致的 device_id。"""


@dataclass(slots=True)
class SinkDiff:
    """新旧配置间的 Sink 差异。"""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TaskDiff:
    """新旧配置间的 Task Definition 差异。"""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConfigDiff:
    """compute_diff 生成的完整配置差异。"""

    devices: DeviceDiff = field(default_factory=DeviceDiff)
    sinks: SinkDiff = field(default_factory=SinkDiff)
    tasks: TaskDiff = field(default_factory=TaskDiff)

    points_changed: bool = False
    """任一点表变化时为 True，触发点映射重注入。"""

    point_tables_changed: list[str] = field(default_factory=list)
    """发生变化（新增/删除/内容/元数据修改）的点表名——Runtime 据此对绑定
    这些表的设备做点映射重注入（不重建 Protocol 连接）。"""

    @property
    def has_any_changes(self) -> bool:
        """任一会影响配置快照的字段发生变化时返回 True。"""
        return bool(
            self.devices.added
            or self.devices.removed
            or self.devices.updated
            or self.sinks.added
            or self.sinks.removed
            or self.sinks.updated
            or self.tasks.added
            or self.tasks.removed
            or self.tasks.updated
            or self.points_changed
        )


@dataclass(slots=True)
class ReloadResult:
    """一次配置热重载的结果。"""

    success: bool
    """运行时重构无错误时为 True。"""

    diff: ConfigDiff
    """本次计算并尝试应用的 ConfigDiff。"""

    errors: list[str] = field(default_factory=list)
    """部分应用或失败时的错误明细。"""

    duration_ms: float = 0.0
    """本次 reload 墙钟耗时，毫秒。"""

    reloaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    """reload 完成时间，UTC。"""


def _device_signature(config: CollectorConfig, device_id: object) -> object:
    """设备的热重载比较签名：聚合 + 协议参数 + 点表绑定。"""
    core = config.core
    device = core.devices[device_id]  # type: ignore[index]
    return (
        device,
        dict(core.device_options_for(device_id)),  # type: ignore[arg-type]
        core.point_table_for_device(device_id).point_table_id,  # type: ignore[arg-type]
    )


def compute_diff(old: CollectorConfig, new: CollectorConfig) -> ConfigDiff:
    """计算两个完整配置快照的结构化差异。"""
    old_devices, new_devices = old.core.devices, new.core.devices
    old_ids, new_ids = set(old_devices), set(new_devices)
    devices = DeviceDiff(
        added=sorted(str(d) for d in new_ids - old_ids),
        removed=sorted(str(d) for d in old_ids - new_ids),
    )
    common = old_ids & new_ids
    devices.updated = sorted(
        str(device_id)
        for device_id in common
        if _device_signature(old, device_id) != _device_signature(new, device_id)
    )
    devices.unchanged = sorted(str(d) for d in common if str(d) not in devices.updated)

    old_sinks, new_sinks = old.sinks, new.sinks
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

    old_tasks, new_tasks = old.tasks, new.tasks
    old_task_ids, new_task_ids = set(old_tasks), set(new_tasks)
    tasks = TaskDiff(
        added=sorted(new_task_ids - old_task_ids),
        removed=sorted(old_task_ids - new_task_ids),
    )
    tasks.updated = sorted(
        task_id
        for task_id in old_task_ids & new_task_ids
        if old_tasks[task_id] != new_tasks[task_id]
    )
    tasks.unchanged = sorted((old_task_ids & new_task_ids) - set(tasks.updated))

    # 点表比较连带进程级点位元数据——point_groups 变化同样影响选点与
    # Task 展开，必须与表内容变化同等对待。
    old_tables, new_tables = old.core.point_tables, new.core.point_tables
    table_ids = set(old_tables) | set(new_tables)
    changed_tables = sorted(
        str(table_id)
        for table_id in table_ids
        if table_id not in old_tables
        or table_id not in new_tables
        or old_tables[table_id] != new_tables[table_id]
        or old.table_meta(table_id) != new.table_meta(table_id)
    )

    return ConfigDiff(
        devices=devices,
        sinks=sinks,
        tasks=tasks,
        points_changed=bool(changed_tables),
        point_tables_changed=changed_tables,
    )
