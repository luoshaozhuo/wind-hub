"""通用 YAML Config Adapter——ConfigPort 的文件系统实现。

职责：YAML 文件读取、反序列化、Config VO 构建、Config VO 序列化、
原子写入、文件错误转换，以及既有 fingerprint / snapshot 一致性机制。
严格的字段校验由 :mod:`core.infrastructure.config.codec` 完成。

读取按主题独立进行——读取一个主题不会隐式加载其他主题文件。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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
from .yaml import (
    _TOPIC_FILES,
    fingerprint_config_set,
    fingerprint_config_topics,
    read_yaml_mapping,
    write_yaml_mapping_atomic,
)

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

        保存语义：配置以规范化完整形式输出——点表输出继承展开后的
        完整点位（不含 extends/remove_points 声明）。
        """
        expected = TOPIC_CONFIG_TYPES[topic]
        if not isinstance(config, expected):
            raise ConfigError(
                f"topic '{topic}' expects config of type {expected.__name__}, "
                f"got {type(config).__name__}"
            )
        write_yaml_mapping_atomic(self._base / _TOPIC_FILES[topic], _DUMPERS[topic](config))

    def fingerprint(self) -> str:
        """整个配置目录的稳定指纹（跨进程配置版本契约）。"""
        return fingerprint_config_set(self._base)

    def fingerprint_topics(self, topics: Iterable[ConfigTopic]) -> str:
        """仅覆盖指定主题文件的指纹；用于进程内部的版本一致性检查。"""
        return fingerprint_config_topics(self._base, topics)

    def open_snapshot(self, topics: Iterable[ConfigTopic]) -> YamlConfigSnapshot:
        """开启只覆盖指定主题的一致性读取会话。"""
        return YamlConfigSnapshot(self._base, topics)


class YamlConfigSnapshot:
    """只覆盖声明主题的一致性读取会话。

    open 时按声明主题逐个捕获配置内容；会话内所有类型化读取都解析
    自捕获内容，不会在会话内静默混用不同版本。捕获窗口内的外部
    并发写入由 :meth:`verify_unchanged` 兜底检测——检测失败即抛错
    中止，不输出混合版本配置。快照不宣称跨文件原子：捕获仍是逐
    文件顺序读取。
    """

    def __init__(self, config_dir: str | Path, topics: Iterable[ConfigTopic]) -> None:
        self._base = Path(config_dir)
        self._topics = tuple(dict.fromkeys(topics))
        if not self._topics:
            raise ConfigError("config snapshot requires at least one topic")
        # 捕获前后双指纹乐观一致性检查：顺序捕获不是跨文件原子操作，
        # 若捕获窗口内已读文件被外部修改，两次指纹不一致，拒绝创建快照，
        # 避免 captured 旧内容与指纹对应的新内容混用导致后续
        # verify_unchanged 误判通过。这不是严格原子快照——并发写入恰
        # 落在两次指纹计算之间时仍依赖 verify_unchanged 兜底。
        fingerprint_before = fingerprint_config_topics(self._base, self._topics)
        captured = {
            topic: read_yaml_mapping(self._base / _TOPIC_FILES[topic])
            for topic in self._topics
        }
        fingerprint_after = fingerprint_config_topics(self._base, self._topics)
        if fingerprint_before != fingerprint_after:
            raise ConfigError(
                "config changed while capturing snapshot: "
                f"before={fingerprint_before} after={fingerprint_after}"
            )
        self._captured = captured
        self._opened_fingerprint = fingerprint_after

    def read(self, topic: ConfigTopic) -> ConfigValue:
        """读取指定主题的配置 VO；未声明主题抛 ConfigError。"""
        captured = self._captured.get(topic)
        if captured is None:
            raise ConfigError(
                f"config snapshot does not include '{_TOPIC_FILES[topic]}'; "
                "declare the topic when opening the snapshot"
            )
        return _PARSERS[topic](dict(captured))

    def verify_unchanged(self) -> None:
        """校验声明主题自 open 以来未被外部修改。"""
        current = fingerprint_config_topics(self._base, self._topics)
        if current != self._opened_fingerprint:
            raise ConfigError(
                "config changed while reading snapshot: "
                f"opened={self._opened_fingerprint} current={current}"
            )


__all__ = ["YamlConfigAdapter", "YamlConfigSnapshot"]
