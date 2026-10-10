"""配置 diff 与热重载结果模型（Application 层）。

compute_diff 比较两个 :class:`CollectorConfig` 快照，产出结构化差异，
驱动 CollectorRuntime.reconfigure 的最小化重构。基础比较由各配置 VO
自身的 ``diff`` 方法按业务 ID 粒度完成；本模块只把语义差异翻译为
Collector 的运行时重构决策（设备/Sink/Task 增删改、点表重注入、
运行参数应用），不触碰任何运行时组件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

from core.application import ConfigError
from core.application.port import ConfigTopic
from core.application.sink_config import SinksConfig
from core.domain.config import (
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    TasksConfig,
)

from .config import CollectorConfig, RuntimeParams

#: 构造期固化、无安全在线迁移方案的 RuntimeParams 字段——变化必须重启进程。
#:
#: - ``queue_maxsize``：asyncio.Queue 按启动值创建，运行中改容量会丢队/溢队；
#: - ``read_timeout``：AcquisitionEngine 构造期固定，引擎不提供更新接口；
#: - ``read_retries`` / ``retry_interval``：装配期固化进 ProtocolRegistry
#:   的 RecoverySettings，既有 RecoveringProtocol 实例不随快照更新。
RESTART_REQUIRED_RUNTIME_FIELDS: tuple[str, ...] = (
    "queue_maxsize",
    "read_timeout",
    "read_retries",
    "retry_interval",
)

#: 可安全热更新的 RuntimeParams 字段——owner 在使用点动态读取当前值：
#: ``backpressure_policy``（SinkRuntime 每次派发读取）、
#: ``shutdown_timeout``（仅影响后续 stop/remove）、``connect_timeout``
#: （DeviceRuntime 每次 connect 读取）。activate 时整体替换 owner 持有的
#: params 快照即真实生效。
HOT_RELOADABLE_RUNTIME_FIELDS: tuple[str, ...] = (
    "backpressure_policy",
    "shutdown_timeout",
    "connect_timeout",
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


#: compute_diff 消费的主题说明：SYSTEM 由 RuntimeParams/ADS 身份的专门比较覆盖，
#: UNITS 的 symbol/name 变化不改变任何装配结果与已解析引用——unit id 增删
#: 会被加载期引用校验拦截，因此不参与运行时差异。


def _topic_config(
    config: CollectorConfig, topic: ConfigTopic, expected: type[_T]
) -> _T:
    value = config.configs.get(topic)
    if value is None:
        raise ConfigError(
            f"collector config snapshot lacks the '{topic}' config VO baseline; "
            "load configs via load_collector_config before computing diffs"
        )
    if not isinstance(value, expected):
        raise ConfigError(
            f"unexpected {topic} config type: {type(value).__name__}"
        )
    return value


_T = TypeVar("_T")


def compute_diff(old: CollectorConfig, new: CollectorConfig) -> ConfigDiff:
    """计算两个完整配置快照的结构化差异。

    基础比较由各配置 VO 的 ``diff`` 方法按业务 ID 粒度完成；本函数只把
    语义差异翻译为 Collector 的运行时重构决策（设备/Sink/Task 增删改、
    点表重注入、运行参数应用），不触碰任何运行时组件。
    """
    old_devices = _topic_config(old, ConfigTopic.DEVICES, DevicesConfig)
    new_devices = _topic_config(new, ConfigTopic.DEVICES, DevicesConfig)
    old_models = _topic_config(old, ConfigTopic.DEVICE_MODELS, DeviceModelsConfig)
    new_models = _topic_config(new, ConfigTopic.DEVICE_MODELS, DeviceModelsConfig)
    devices_diff = old_devices.diff(new_devices)
    models_diff = old_models.diff(new_models)

    old_enabled = {d.device_id for d in old_devices.devices.values() if d.enabled}
    new_enabled = {d.device_id for d in new_devices.devices.values() if d.enabled}

    devices = DeviceDiff(
        # enabled 翻转等价于运行时索引的增删：disabled 设备不进索引。
        added=sorted(new_enabled - old_enabled),
        removed=sorted(old_enabled - new_enabled),
    )
    common = old_enabled & new_enabled
    modified_ids = set(devices_diff.changed) & common
    # 型号变化（连接默认值 / 点表绑定 / 协议）影响其全部实例的设备签名。
    changed_models = {
        key.removeprefix("device_models.")
        for key in (*models_diff.added, *models_diff.changed)
        if key.startswith("device_models.")
    }
    model_by_device = {
        d.device_id: d.model
        for d in (*old_devices.devices.values(), *new_devices.devices.values())
    }
    devices.updated = sorted(
        modified_ids
        | {device_id for device_id in common if model_by_device.get(device_id) in changed_models}
    )
    devices.unchanged = sorted(common - set(devices.updated))

    # 点表整表粒度比较——任一测点/元数据变化触发整表重注入。
    points_diff = _topic_config(old, ConfigTopic.POINTS, PointTablesConfig).diff(
        _topic_config(new, ConfigTopic.POINTS, PointTablesConfig)
    )
    changed_tables = sorted(
        set(points_diff.added) | set(points_diff.removed) | set(points_diff.changed)
    )

    old_sinks = _topic_config(old, ConfigTopic.SINKS, SinksConfig)
    new_sinks = _topic_config(new, ConfigTopic.SINKS, SinksConfig)
    sinks_diff = old_sinks.diff(new_sinks)
    sinks = SinkDiff(
        added=sorted(sinks_diff.added),
        removed=sorted(sinks_diff.removed),
    )
    common_sinks = {sink.name for sink in old_sinks.sinks} & {
        sink.name for sink in new_sinks.sinks
    }
    sink_updated = set(sinks_diff.changed) & common_sinks
    # 点表内容或设备→型号→点表绑定的变化可能改变 Sink 点的解析结果
    # （缺省 datatype/unit 继承源点、source 引用换表）。
    affected_tables = set(changed_tables) | _bound_tables_of_models(old, new, changed_models)
    if affected_tables:
        sink_updated |= _sinks_bound_to_tables(old, new, affected_tables) & common_sinks
    sinks.updated = sorted(sink_updated)
    sinks.unchanged = sorted(common_sinks - set(sinks.updated))

    tasks_diff = _topic_config(old, ConfigTopic.TASKS, TasksConfig).diff(
        _topic_config(new, ConfigTopic.TASKS, TasksConfig)
    )
    old_task_ids = set(old.tasks)
    new_task_ids = set(new.tasks)
    tasks = TaskDiff(
        added=sorted(new_task_ids - old_task_ids),
        removed=sorted(old_task_ids - new_task_ids),
    )
    tasks.updated = sorted(set(tasks_diff.changed) & old_task_ids & new_task_ids)
    tasks.unchanged = sorted((old_task_ids & new_task_ids) - set(tasks.updated))

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


def _bound_tables_of_models(
    old: CollectorConfig, new: CollectorConfig, changed_models: set[str]
) -> set[str]:
    """变化型号在新旧配置中绑定的全部点表名。"""
    tables: set[str] = set()
    for config in (old, new):
        models = config.configs.get(ConfigTopic.DEVICE_MODELS)
        if not isinstance(models, DeviceModelsConfig):
            continue
        for model_id in changed_models:
            model = models.device_models.get(model_id)
            if model is not None:
                tables.add(model.point_table)
    return tables


def _sinks_bound_to_tables(
    old: CollectorConfig, new: CollectorConfig, changed_tables: set[str]
) -> set[str]:
    """返回引用了变化点表（经 device → model → point_table 绑定）的 Sink 名。"""
    names: set[str] = set()
    for config in (old, new):
        models = config.configs.get(ConfigTopic.DEVICE_MODELS)
        devices = config.configs.get(ConfigTopic.DEVICES)
        sinks = config.configs.get(ConfigTopic.SINKS)
        if not isinstance(models, DeviceModelsConfig) or not isinstance(
            devices, DevicesConfig
        ) or not isinstance(sinks, SinksConfig):
            continue
        table_by_device = {
            d.device_id: models.device_models[d.model].point_table
            for d in devices.devices.values()
            if d.model in models.device_models
        }
        for sink in sinks.sinks:
            if any(
                table_by_device.get(point.source.device_id) in changed_tables
                for point in sink.points
            ):
                names.add(sink.name)
    return names
