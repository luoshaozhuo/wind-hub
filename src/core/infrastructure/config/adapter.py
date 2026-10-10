"""通用 YAML Config Adapter——ConfigPort 的文件系统实现。

职责：YAML 文件读取、反序列化、Config VO 构建、Config VO 序列化、
单文件原子写入、文件错误转换。严格的字段校验由
:mod:`core.infrastructure.config.codec` 完成。

读取按主题独立进行——读取一个主题不会隐式加载其他主题文件。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from core.application import ConfigError
from core.application.port import TOPIC_CONFIG_TYPES, ConfigTopic, ConfigValue

from .codec import (
    dump_device_models_config,
    dump_devices_config,
    dump_point_tables_config,
    dump_sinks_config,
    dump_system_config,
    dump_tasks_config,
    dump_units_config,
    parse_device_models_config,
    parse_devices_config,
    parse_point_tables_config,
    parse_sinks_config,
    parse_system_config,
    parse_tasks_config,
    parse_units_config,
)
from .yaml import _TOPIC_FILES, read_yaml_mapping, write_yaml_mapping_atomic

_PARSERS: dict[ConfigTopic, Callable[[Mapping[str, Any]], ConfigValue]] = {
    ConfigTopic.SYSTEM: parse_system_config,
    ConfigTopic.DEVICE_MODELS: parse_device_models_config,
    ConfigTopic.DEVICES: parse_devices_config,
    ConfigTopic.POINTS: parse_point_tables_config,
    ConfigTopic.UNITS: parse_units_config,
    ConfigTopic.TASKS: parse_tasks_config,
    ConfigTopic.SINKS: parse_sinks_config,
}

_DUMPERS: dict[ConfigTopic, Callable[[Any], dict[str, Any]]] = {
    ConfigTopic.SYSTEM: dump_system_config,
    ConfigTopic.DEVICE_MODELS: dump_device_models_config,
    ConfigTopic.DEVICES: dump_devices_config,
    ConfigTopic.POINTS: dump_point_tables_config,
    ConfigTopic.UNITS: dump_units_config,
    ConfigTopic.TASKS: dump_tasks_config,
    ConfigTopic.SINKS: dump_sinks_config,
}


class YamlConfigAdapter:
    """一次构造确定配置根目录；按主题读写配置 VO。"""

    def __init__(self, config_dir: str | Path) -> None:
        self._base = Path(config_dir)

    def read(self, topic: ConfigTopic) -> ConfigValue:
        """读取指定主题的配置 VO；不隐式加载其他主题文件。"""
        raw = read_yaml_mapping(self._base / _TOPIC_FILES[topic])
        return _PARSERS[topic](raw)

    def save(self, topic: ConfigTopic, config: ConfigValue) -> None:
        """原子写入指定主题；topic 与 config 类型必须匹配。

        每次只保存一个主题的配置；写入原子性（临时文件 + fsync +
        原子替换）保证失败不破坏原文件。保存语义：配置以规范化完整
        形式输出——点表输出继承展开后的完整点位（不含
        extends/remove_points 声明）。
        """
        expected = TOPIC_CONFIG_TYPES[topic]
        if not isinstance(config, expected):
            raise ConfigError(
                f"topic '{topic}' expects config of type {expected.__name__}, "
                f"got {type(config).__name__}"
            )
        write_yaml_mapping_atomic(self._base / _TOPIC_FILES[topic], _DUMPERS[topic](config))


__all__ = ["YamlConfigAdapter"]
