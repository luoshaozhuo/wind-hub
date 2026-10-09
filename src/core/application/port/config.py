"""配置端口：按主题读写类型化配置 VO，以及一致性读取会话。

调用方只读取自身需要的主题；读取点表不会隐式加载无关的 Sink/Task
配置。公开契约只暴露明确的配置 VO 联合类型，不暴露无约束
``dict[str, Any]``，也不使用 ``Any`` 作为返回类型。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeAlias, runtime_checkable

from core.application.sink_config import SinksConfig
from core.domain.config import (
    ConfigTopic,
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    SystemConfig,
    TasksConfig,
    UnitsConfig,
)

#: 全部配置主题的值对象联合类型——ConfigPort/ConfigService 的公开类型契约。
ConfigValue: TypeAlias = (
    SystemConfig
    | DeviceModelsConfig
    | DevicesConfig
    | PointTablesConfig
    | UnitsConfig
    | TasksConfig
    | SinksConfig
)

#: 主题 → 配置 VO 类型；用于 topic 与 config 类型匹配检查。
TOPIC_CONFIG_TYPES: dict[ConfigTopic, type] = {
    ConfigTopic.SYSTEM: SystemConfig,
    ConfigTopic.DEVICE_MODELS: DeviceModelsConfig,
    ConfigTopic.DEVICES: DevicesConfig,
    ConfigTopic.POINTS: PointTablesConfig,
    ConfigTopic.UNITS: UnitsConfig,
    ConfigTopic.TASKS: TasksConfig,
    ConfigTopic.SINKS: SinksConfig,
}


@runtime_checkable
class ConfigPort(Protocol):
    """按主题读写配置 VO 的端口；具体 YAML/文件实现属于 Adapter。"""

    def read(self, topic: ConfigTopic) -> ConfigValue:
        """读取指定主题的配置 VO；失败抛 ConfigError。"""
        ...

    def save(self, topic: ConfigTopic, config: ConfigValue) -> None:
        """持久化指定主题的配置 VO；失败抛 ConfigError 且不得部分写入。"""
        ...


@runtime_checkable
class ConfigSnapshot(Protocol):
    """一次配置读取会话：会话内多次读取解析自同一批捕获内容。

    - 会话内不混用不同版本：读取一律来自 open 时捕获的内容；
    - 依赖范围显式：读取未声明主题立即报错，不会隐式读盘；
    - 外部并发修改由 :meth:`verify_unchanged` 检测，失败即中止，
      不输出混合版本配置。快照不宣称跨文件原子：捕获仍是逐文件
      顺序读取，verify 用于兜底检测捕获窗口内的写入。
    """

    def read(self, topic: ConfigTopic) -> ConfigValue:
        """读取指定主题的配置 VO；未声明主题抛 ConfigError。"""
        ...

    def verify_unchanged(self) -> None:
        """校验声明主题自 open 以来未被外部修改；被修改则抛 ConfigError。"""
        ...


@runtime_checkable
class ConfigSnapshotPort(Protocol):
    """支持一致性读取会话的配置端口扩展能力。"""

    def open_snapshot(self, topics: Iterable[ConfigTopic]) -> ConfigSnapshot:
        """开启只覆盖指定主题的一致性读取会话。"""
        ...


__all__ = [
    "TOPIC_CONFIG_TYPES",
    "ConfigPort",
    "ConfigSnapshot",
    "ConfigSnapshotPort",
    "ConfigTopic",
    "ConfigValue",
]
