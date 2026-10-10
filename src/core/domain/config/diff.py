"""配置语义差异：``diff(old, new)`` 纯函数与结果模型。

只描述「哪里变了、原来是什么、现在是什么」，不包含任何运行时操作指令
（重连、重订阅、任务重启等业务判断属于各应用模块）。``diff`` 不读取
YAML、不访问文件系统、不修改输入对象，也不依赖 Adapter 或 Service；
结果不可变，不保留对可变源数据结构的引用。

比较规则：

1. 相同路径、相同语义值：无变化；
2. 旧不存在、新存在：added（完整对象按对象路径整体记录，不展开叶子）；
3. 旧存在、新不存在：removed（同上）；
4. 两边都存在但值不同：changed，定位到实际变化字段；
5. Mapping 按键比较，与插入顺序无关；
6. Device/Task/Sink 等具有业务 ID 的模型序列按 ID 比较，不按列表位置
   （序列全部元素为含唯一业务 ID 属性的配置模型时按 ID 对齐，否则按
   位置比较）；
7. 其余序列按位置比较；
8. 数值使用严格配置语义（类型与值都相等才算相同），不使用浮点近似容差。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeAlias

from pydantic import BaseModel

from .models import (
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    SystemConfig,
    TasksConfig,
    UnitsConfig,
    freeze_config_value,
)

#: 业务 ID 属性候选——模型序列元素按首个命中且唯一的属性对齐。
_ID_FIELDS: tuple[str, ...] = ("device_id", "task_id", "name")

#: Domain 层的配置 VO 联合类型（SinksConfig 属于 Application 层，
#: 以 pydantic BaseModel 形式参与比较，见 :func:`diff` 的类型契约）。
_DomainConfig: TypeAlias = (
    SystemConfig
    | DeviceModelsConfig
    | DevicesConfig
    | PointTablesConfig
    | UnitsConfig
    | TasksConfig
)

_DIFFABLE_TYPES: tuple[type, ...] = (
    SystemConfig,
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    UnitsConfig,
    TasksConfig,
    BaseModel,
)


@dataclass(frozen=True, slots=True)
class ValueChange:
    """同一配置路径上新旧两个语义值。"""

    old: object
    new: object


@dataclass(frozen=True, slots=True)
class ConfigDiff:
    """两个相同具体类型的配置 VO 之间的语义差异。

    键为稳定业务路径（如 ``devices.WTG001.endpoint.host``）：

    - ``added``：旧配置不存在、新配置存在的对象或值；
    - ``removed``：旧配置存在、新配置不存在的对象或值；
    - ``changed``：两边都存在但语义值不同的路径及新旧值。

    新增/删除完整对象时以对象路径整体记录，不展开叶子字段；修改已有
    对象时定位到实际变化的字段路径。
    """

    added: Mapping[str, object] = field(default_factory=dict)
    removed: Mapping[str, object] = field(default_factory=dict)
    changed: Mapping[str, ValueChange] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "added", MappingProxyType(dict(self.added)))
        object.__setattr__(self, "removed", MappingProxyType(dict(self.removed)))
        object.__setattr__(self, "changed", MappingProxyType(dict(self.changed)))

    @property
    def has_any_changes(self) -> bool:
        """任一差异存在时为 True。"""
        return bool(self.added or self.removed or self.changed)


def diff(old: _DomainConfig | BaseModel, new: _DomainConfig | BaseModel) -> ConfigDiff:
    """比较两个配置 VO，返回不可变语义差异；相同配置返回空差异。

    old/new 必须是相同的具体 Config VO 类型（六种 Domain dataclass VO
    或 Application 层的 pydantic ``SinksConfig``）。

    Raises:
        TypeError: old/new 类型不一致，或不是可比较的配置 VO。
    """
    if type(old) is not type(new):
        raise TypeError(
            f"diff requires the same config type, got "
            f"{type(old).__name__} and {type(new).__name__}"
        )
    if not isinstance(old, _DIFFABLE_TYPES):
        raise TypeError(f"diff does not support config of type {type(old).__name__}")
    collector = _DiffCollector()
    collector.compare((), old, new)
    return ConfigDiff(
        added=collector.added,
        removed=collector.removed,
        changed=collector.changed,
    )


class _DiffCollector:
    def __init__(self) -> None:
        self.added: dict[str, object] = {}
        self.removed: dict[str, object] = {}
        self.changed: dict[str, ValueChange] = {}

    def compare(self, path: tuple[str, ...], old: Any, new: Any) -> None:
        if _strict_equal(old, new):
            return
        old_kind = _kind(old)
        new_kind = _kind(new)
        if old_kind != new_kind or old_kind == "scalar":
            self._record_changed(path, old, new)
            return
        if old_kind == "model":
            self._compare_models(path, old, new)
        elif old_kind == "mapping":
            self._compare_mappings(path, old, new)
        else:
            self._compare_sequences(path, old, new)

    def _compare_models(self, path: tuple[str, ...], old: Any, new: Any) -> None:
        if type(old) is not type(new):
            self._record_changed(path, old, new)
            return
        old_fields = _model_field_values(old)
        for name, new_value in _model_field_values(new).items():
            self.compare((*path, name), old_fields[name], new_value)

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
        id_field = _sequence_id_field(old, new)
        if id_field is not None:
            old_by_id = {str(getattr(item, id_field)): item for item in old}
            new_by_id = {str(getattr(item, id_field)): item for item in new}
            self._compare_mappings(path, old_by_id, new_by_id)
            return
        if len(old) != len(new):
            self._record_changed(path, old, new)
            return
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            self.compare((*path, str(index)), old_item, new_item)

    def _record_changed(self, path: tuple[str, ...], old: Any, new: Any) -> None:
        self.changed[_path_str(path)] = ValueChange(old=_frozen(old), new=_frozen(new))


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


def _model_field_values(value: Any) -> dict[str, Any]:
    """模型字段名 → 原始字段值（dataclass 与 pydantic 一致按字段递归比较）。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, BaseModel):
        return {name: getattr(value, name) for name in type(value).model_fields}
    raise TypeError(f"not a model: {type(value).__name__}")


def _sequence_id_field(old: Sequence[Any], new: Sequence[Any]) -> str | None:
    """两个模型序列可共用的业务 ID 属性；不可用（非模型/无候选/不唯一）返回 None。"""
    items = [*old, *new]
    if not items or any(_kind(item) != "model" for item in items):
        return None
    for candidate in _ID_FIELDS:
        old_ids = [getattr(item, candidate, None) for item in old]
        new_ids = [getattr(item, candidate, None) for item in new]
        ids = old_ids + new_ids
        if any(not isinstance(value, str) or not value for value in ids):
            continue
        if len(set(old_ids)) == len(old_ids) and len(set(new_ids)) == len(new_ids):
            return candidate
    return None


def _frozen(value: Any) -> Any:
    """记录到 diff 结果的值：不保留对可变源数据结构的引用。

    - Mapping/list/tuple：递归冻结；
    - pydantic 模型：dump 后冻结（其 list 字段是可变容器）；
    - dataclass 配置 VO 本身深度不可变，直接记录。
    """
    if isinstance(value, BaseModel):
        return freeze_config_value(value.model_dump(mode="python"))
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


__all__ = ["ConfigDiff", "ValueChange", "diff"]
