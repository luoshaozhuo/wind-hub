"""配置语义差异结果模型。

只描述「哪里变了、原来是什么、现在是什么」，不包含任何运行时操作指令
（重连、重订阅、任务重启等业务判断属于各应用模块）。结果不可变，不
保留对可变源数据结构的引用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .models import ConfigTopic


@dataclass(frozen=True, slots=True)
class ValueChange:
    """同一配置路径上新旧两个语义值。"""

    old: object
    new: object


@dataclass(frozen=True, slots=True)
class ConfigDiff:
    """两个同主题配置对象之间的语义差异。

    键为稳定业务路径（如 ``devices.WTG001.endpoint.host``）：

    - ``added``：旧配置不存在、新配置存在的对象或值；
    - ``removed``：旧配置存在、新配置不存在的对象或值；
    - ``modified``：两边都存在但语义值不同的路径及新旧值。

    新增/删除完整对象时以对象路径整体记录，不展开叶子字段；修改已有
    对象时定位到实际变化的字段路径。
    """

    topic: ConfigTopic
    added: Mapping[str, object] = field(default_factory=dict)
    removed: Mapping[str, object] = field(default_factory=dict)
    modified: Mapping[str, ValueChange] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "added", MappingProxyType(dict(self.added)))
        object.__setattr__(self, "removed", MappingProxyType(dict(self.removed)))
        object.__setattr__(self, "modified", MappingProxyType(dict(self.modified)))

    @property
    def has_any_changes(self) -> bool:
        """任一差异存在时为 True。"""
        return bool(self.added or self.removed or self.modified)


__all__ = ["ConfigDiff", "ValueChange"]
