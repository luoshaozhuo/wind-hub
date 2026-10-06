"""tasks.yaml 周期采集任务（Task Definition）配置模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wind_hub_core.model.errors import ConfigError


class TaskTarget(BaseModel):
    """采集 Task 的输出目标——只引用 Sink 名称。

    Sink 实例与连接参数定义在 ``sinks.yaml`` 中；Task 不复制
    任何连接配置。
    """

    model_config = ConfigDict(extra="forbid")

    sink: str
    """目标 Sink 名（``sinks.yaml`` 中的 Sink name）。"""


class CollectionTaskConfig(BaseModel):
    """周期采集 Task 的业务定义（配置层 Task Definition）。

    语义：

    - ``device`` / ``device_group`` 二选一（XOR）——选择设备范围；
    - ``point_group`` 单值必填——选择点位范围（匹配
      ``PointConfig.point_groups`` 多值集合）；
    - ``interval`` 为采集节拍（秒，> 0）——主动轮询协议（Modbus、ADS
      Sum）作为 fixed-rate 采样周期，ADS 订阅作为 notification
      cycle_time；纯 IEC104 订阅 Task 可不配置（数据到达时机由远端
      spontaneous / periodic 决定）。是否必填由加载期跨文件校验按
      命中设备的协议能力判定；
    - ``targets`` 决定采集结果输出到哪些 Sink；
    - ``enabled`` 是配置级能力开关：``False`` 时 Runtime 不创建运行实例。

    ``device_group`` Task 在 Runtime 展开为每台命中设备一个 Task Instance
    （见 ``application/runtime``）。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    device: str | None = None
    """目标单台设备（``device_id``）；与 ``device_group`` 互斥。"""
    device_group: str | None = None
    """目标设备业务类别；与 ``device`` 互斥。"""
    point_group: str
    """点位分组（单值）——命中 ``point_groups`` 含该值的全部点位。"""
    interval: float | None = None
    """采集节拍（秒，配置时必须 > 0）。主动轮询与 ADS 订阅必填；
    纯 IEC104 订阅 Task 可省略。"""
    targets: list[TaskTarget]
    """输出目标 Sink 列表（至少一个，不允许重复）。"""
    enabled: bool = True

    @model_validator(mode="after")
    def _validate_task(self) -> CollectionTaskConfig:
        if not self.task_id.strip():
            raise ConfigError("Collection task: task_id must be non-empty")
        if (self.device is None) == (self.device_group is None):
            raise ConfigError(
                f"Task '{self.task_id}': exactly one of 'device' / 'device_group' "
                "must be configured (XOR)"
            )
        if not self.point_group.strip():
            raise ConfigError(f"Task '{self.task_id}': point_group must be non-empty")
        if self.interval is not None and self.interval <= 0:
            raise ConfigError(f"Task '{self.task_id}': interval must be > 0, got {self.interval}")
        if not self.targets:
            raise ConfigError(f"Task '{self.task_id}': targets must be non-empty")
        sink_names = [t.sink for t in self.targets]
        if len(sink_names) != len(set(sink_names)):
            raise ConfigError(f"Task '{self.task_id}': duplicate target sinks: {sink_names}")
        return self


class TasksConfig(BaseModel):
    """Raw YAML root model——``tasks.yaml`` 顶层 Task Definition 集（文件级 wrapper）。

    ``task_id`` 唯一性在此校验；最终 resolved ``Config.tasks`` 是扁平的
    ``dict[task_id, CollectionTaskConfig]``，不经过本类型。
    """

    model_config = ConfigDict(extra="forbid")

    tasks: list[CollectionTaskConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tasks(self) -> TasksConfig:
        seen: set[str] = set()
        for t in self.tasks:
            if t.task_id in seen:
                raise ConfigError(f"Duplicate task_id: '{t.task_id}'")
            seen.add(t.task_id)
        return self


__all__ = [
    "TaskTarget",
    "CollectionTaskConfig",
    "TasksConfig",
]
