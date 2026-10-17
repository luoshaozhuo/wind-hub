"""配置 diff 与热重载结果模型（Application 层）。

compute_diff 比较两个 :class:`CollectorConfig` 快照，产出结构化差异，
驱动 CollectorRuntime.reconfigure 的最小化重构。
直接比较领域对象与 Sink 契约，并转换为
Collector 的运行时重构决策（设备/Sink/Task 增删改、点表重注入、
运行参数应用），不触碰任何运行时组件。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

from .config import CollectorConfig, RuntimeParams

#: 构造期固化、无安全在线迁移方案的 RuntimeParams 字段——变化必须重启进程。
#:
#: - ``queue_maxsize``：asyncio.Queue 按启动值创建，运行中改容量会丢队/溢队；
#: - ``read_timeout``：AcquisitionEngine 构造期固定，引擎不提供更新接口。
RESTART_REQUIRED_RUNTIME_FIELDS: tuple[str, ...] = (
    "queue_maxsize",
    "read_timeout",
)

#: 可安全热更新的 RuntimeParams 字段——owner 在使用点动态读取当前值：
#: ``backpressure_policy``（SinkRuntime 每次派发读取）、
#: ``shutdown_timeout``（仅影响后续 stop/remove）、``connect_timeout``
#: 以及 Watchdog 时序参数均在运行时 owner 使用当前快照。activate 时整体
#: 替换 params 即真实生效。
HOT_RELOADABLE_RUNTIME_FIELDS: tuple[str, ...] = (
    "backpressure_policy",
    "shutdown_timeout",
    "connect_timeout",
    "watchdog_interval",
    "health_timeout",
)


def restart_required_runtime_fields(old: RuntimeParams, new: RuntimeParams) -> list[str]:
    """返回新旧 RuntimeParams 间发生变化的 restart-required 字段名（排序稳定）。"""
    return sorted(
        name for name in RESTART_REQUIRED_RUNTIME_FIELDS if getattr(old, name) != getattr(new, name)
    )


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

    runtime_changed: bool = False
    """可热更新的 RuntimeParams 字段发生变化时为 True，activate 时把新
    params 快照应用到运行时 owner（restart-required 字段在 prepare 阶段
    已被拒绝，不会出现在本 diff 中）。"""

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
            or self.runtime_changed
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


_K = TypeVar("_K")
_V = TypeVar("_V")


def _differences(
    old: Mapping[_K, _V], new: Mapping[_K, _V]
) -> tuple[list[str], list[str], list[str], list[str]]:
    """返回 added、removed、updated、unchanged，统一各类配置的比较规则。"""
    before, after = set(old), set(new)
    common = before & after
    return (
        sorted(str(key) for key in after - before),
        sorted(str(key) for key in before - after),
        sorted(str(key) for key in common if old[key] != new[key]),
        sorted(str(key) for key in common if old[key] == new[key]),
    )


def compute_diff(old: CollectorConfig, new: CollectorConfig) -> ConfigDiff:
    """直接比较领域对象与已解析的运行配置，不依赖旧主题 VO。"""
    old_devices, new_devices = old.devices, new.devices
    added, removed, changed, _ = _differences(old_devices, new_devices)
    common = set(old_devices) & set(new_devices)
    updated = set(changed)
    for device_id in common:
        previous = old_devices[device_id]
        current = new_devices[device_id]
        if (
            old.device_models.get(previous.device_model_id)
            != new.device_models.get(current.device_model_id)
            or old.point_table_for_device(device_id) != new.point_table_for_device(device_id)
        ):
            updated.add(str(device_id))
    devices = DeviceDiff(
        added=added,
        removed=removed,
        updated=sorted(updated),
        unchanged=sorted(str(item) for item in common if str(item) not in updated),
    )

    changed_tables = sorted(
        str(key)
        for key in set(old.point_tables) | set(new.point_tables)
        if old.point_tables.get(key) != new.point_tables.get(key)
    )
    added_sinks, removed_sinks, changed_sinks, unchanged_sinks = _differences(old.sinks, new.sinks)
    # 下游 Sink 引用可能因设备映射或点表变化而改变，直接比较解析后契约。
    sinks = SinkDiff(
        added=added_sinks,
        removed=removed_sinks,
        updated=changed_sinks,
        unchanged=unchanged_sinks,
    )
    added_tasks, removed_tasks, changed_tasks, unchanged_tasks = _differences(old.tasks, new.tasks)
    tasks = TaskDiff(
        added=added_tasks,
        removed=removed_tasks,
        updated=changed_tasks,
        unchanged=unchanged_tasks,
    )
    return ConfigDiff(
        devices=devices,
        sinks=sinks,
        tasks=tasks,
        points_changed=bool(changed_tables),
        point_tables_changed=changed_tables,
        runtime_changed=any(
            getattr(old.runtime, name) != getattr(new.runtime, name)
            for name in HOT_RELOADABLE_RUNTIME_FIELDS
        ),
    )
