"""共享语义配置 Diff 用例。

对两个同主题的完整配置 VO 执行语义比较，产出 :class:`ConfigDiff`。
只描述「哪里变了、原来是什么、现在是什么」——不读取 YAML、不修改
输入、不访问业务运行时，也不输出任何运行时操作指令（重连/重订阅/
任务重启等判断属于 Collector/Commander）。

比较规则：

1. 相同路径、相同语义值：无变化；
2. 旧不存在、新存在：added（完整对象按对象路径整体记录，不展开叶子）；
3. 旧存在、新不存在：removed（同上）；
4. 两边都存在但值不同：modified，定位到实际变化字段；
5. Mapping 按键比较，与插入顺序无关；
6. Device/Task/Sink 等具有 ID 的集合按 ID 比较，不按列表位置；
7. 有明确顺序语义的序列按顺序比较；
8. 数值使用严格配置语义（类型与值都相等才算相同），不使用浮点近似容差。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from core.application.port.config import TOPIC_CONFIG_TYPES, ConfigValue
from core.application.sink_config import SinksConfig
from core.domain.config import (
    ConfigDiff,
    ConfigTopic,
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    TasksConfig,
    UnitsConfig,
    ValueChange,
    freeze_config_value,
)


class DiffConfigUseCase:
    """公共语义差异识别用例——Collector/Commander 共用的基础 Config 比较。"""

    def execute(
        self,
        topic: ConfigTopic,
        old: ConfigValue,
        new: ConfigValue,
    ) -> ConfigDiff:
        """比较两个同主题配置 VO，返回不可变语义差异。

        Raises:
            TypeError: old/new 与 topic 期望的 VO 类型不匹配。
        """
        expected = TOPIC_CONFIG_TYPES[topic]
        for label, config in (("old", old), ("new", new)):
            if not isinstance(config, expected):
                raise TypeError(
                    f"diff '{topic}': {label} must be {expected.__name__}, "
                    f"got {type(config).__name__}"
                )
        collector = _DiffCollector()
        old_root = _normalize_root(topic, old)
        new_root = _normalize_root(topic, new)
        collector.compare((topic.value,), old_root, new_root)
        return ConfigDiff(
            topic=topic,
            added=collector.added,
            removed=collector.removed,
            modified=collector.modified,
        )


def _normalize_root(topic: ConfigTopic, config: ConfigValue) -> Any:
    """把主题根 VO 规范化为业务路径友好的结构。

    - 具有 ID 的集合（Device/Task/Sink）→ ``{ID: 对象}``，与列表位置无关；
    - 单字段包装 VO（PointTablesConfig/UnitsConfig）→ 直接展开内部映射，
      路径形如 ``points.<表名>.points.<point_id>``、``units.<单位ID>``；
    - DeviceModelsConfig → 型号按 ID 置于根级，设备类型置于
      ``device_types`` 键下，路径形如 ``device_models.<型号ID>.protocol``。
    """
    if isinstance(config, DevicesConfig):
        return {device.device_id: device for device in config.devices}
    if isinstance(config, TasksConfig):
        return {task.task_id: task for task in config.tasks}
    if isinstance(config, SinksConfig):
        return {
            sink.name: _freeze_dump(sink.model_dump(mode="python")) for sink in config.sinks
        }
    if isinstance(config, PointTablesConfig):
        return config.tables
    if isinstance(config, UnitsConfig):
        return config.units
    if isinstance(config, DeviceModelsConfig):
        return {"device_types": config.device_types, **config.device_models}
    return config


def _freeze_dump(value: Any) -> Any:
    """冻结 pydantic dump 结果，避免 diff 保留可变源结构引用。"""
    return freeze_config_value(value)


class _DiffCollector:
    def __init__(self) -> None:
        self.added: dict[str, object] = {}
        self.removed: dict[str, object] = {}
        self.modified: dict[str, ValueChange] = {}

    def compare(self, path: tuple[str, ...], old: Any, new: Any) -> None:
        if _strict_equal(old, new):
            return
        old_kind = _kind(old)
        new_kind = _kind(new)
        if old_kind != new_kind or old_kind == "scalar":
            self._record_modified(path, old, new)
            return
        if old_kind == "model":
            self._compare_models(path, old, new)
        elif old_kind == "mapping":
            self._compare_mappings(path, old, new)
        else:
            self._compare_sequences(path, old, new)

    def _compare_models(self, path: tuple[str, ...], old: Any, new: Any) -> None:
        if type(old) is not type(new):
            self._record_modified(path, old, new)
            return
        old_fields = _model_fields(old)
        for name in _model_fields(new):
            self.compare((*path, name), old_fields[name], getattr(new, name))

    def _compare_mappings(
        self, path: tuple[str, ...], old: Mapping[str, Any], new: Mapping[str, Any]
    ) -> None:
        old_keys = set(old)
        new_keys = set(new)
        for key in sorted(new_keys - old_keys, key=str):
            self.added[_path_str((*path, str(key)))] = _frozen(new[key])
        for key in sorted(old_keys - new_keys, key=str):
            self.removed[_path_str((*path, str(key)))] = _frozen(old[key])
        for key in sorted(old_keys & new_keys, key=str):
            self.compare((*path, str(key)), old[key], new[key])

    def _compare_sequences(
        self, path: tuple[str, ...], old: Sequence[Any], new: Sequence[Any]
    ) -> None:
        if len(old) != len(new):
            self._record_modified(path, old, new)
            return
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            self.compare((*path, str(index)), old_item, new_item)

    def _record_modified(self, path: tuple[str, ...], old: Any, new: Any) -> None:
        self.modified[_path_str(path)] = ValueChange(old=_frozen(old), new=_frozen(new))


def _path_str(parts: tuple[str, ...]) -> str:
    return ".".join(parts)


def _kind(value: Any) -> str:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return "model"
    if isinstance(value, BaseModel):
        return "model"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, tuple | list):
        return "sequence"
    return "scalar"


def _model_fields(value: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if isinstance(value, BaseModel):
        dumped: dict[str, Any] = _freeze_dump(value.model_dump(mode="python"))
        return dumped
    raise TypeError(f"not a model: {type(value).__name__}")


def _frozen(value: Any) -> Any:
    """记录到 diff 结果的值：Mapping/tuple 递归冻结，VO 本身已不可变。"""
    if isinstance(value, Mapping | list | tuple):
        return freeze_config_value(value)
    return value


def _strict_equal(old: Any, new: Any) -> bool:
    """严格配置语义：标量要求类型与值都相等；容器交给递归比较。"""
    old_kind = _kind(old)
    if old_kind != _kind(new):
        return False
    if old_kind == "scalar":
        return type(old) is type(new) and old == new
    if old_kind == "model":
        return type(old) is type(new) and old == new
    if old_kind == "mapping":
        if set(old) != set(new):
            return False
        return all(_strict_equal(old[key], new[key]) for key in old)
    return len(old) == len(new) and all(
        _strict_equal(a, b) for a, b in zip(old, new, strict=True)
    )


__all__ = ["DiffConfigUseCase"]
