"""配置端口：按主题读取类型化配置 VO。

ConfigPort 是只读契约——持久化（save）属于具体 Adapter（如
:class:`~core.infrastructure.config.YamlConfigAdapter`），不进入 Port。
公开契约只暴露明确的配置 VO 联合类型，不暴露无约束
``dict[str, Any]``，也不使用 ``Any`` 作为返回类型。
"""

from __future__ import annotations

from typing import Protocol, TypeAlias, runtime_checkable

from core.application.config_snapshot import ConfigSnapshot
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

#: 全部配置主题的值对象联合类型——ConfigPort 的公开类型契约。
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
    """全量配置加载端口；按主题旧接口仅供迁移期兼容。"""

    def load(self) -> ConfigSnapshot:
        """一次性读取并校验全部配置，返回完整领域快照。"""
        ...

    def read(self, topic: ConfigTopic) -> ConfigValue:
        """读取指定主题的配置 VO；失败抛 ConfigError。"""
        ...


__all__ = [
    "TOPIC_CONFIG_TYPES",
    "ConfigPort",
    "ConfigTopic",
    "ConfigValue",
]
