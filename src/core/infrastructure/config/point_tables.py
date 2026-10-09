"""Point Table 单继承解析——继承声明草稿 → 继承展开后的完整点表 VO。

:class:`PointPatch` / :class:`PointTableDraft` 只是解释未展开 YAML 配置
的输入结构（以 ``declared`` 区分「未写」与「显式 null」），不是与
Domain Config 平行的第二套配置模型；解析结果一律为
:class:`~core.domain.config.PointTableConfig`。

解析流程（对每张表，带 cache 与 cycle 检测）::

    resolve_table(name, stack):
        cache 命中 → 直接返回
        name 已在 stack → ConfigError（cycle path）
        extends → 递归解析父表并拷贝其结果
        protocol → 基础表必填；子表缺省继承父表，显式配置必须与父表一致
        remove_points → 逐个点校验存在后删除
        points → point_id 已存在则 merge（override），否则新建（append）
        全部结果构造为完整 PointConfig（VO 不变量兜底校验）

merge 语义与旧系统一致：以补丁实际写出的字段（含显式 null）覆盖父值，
未写字段继承父值；``address`` 为整体覆盖；点位顺序保持声明顺序。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.application.errors import ConfigError
from core.domain.config import PointConfig, PointTableConfig, freeze_config_value

_PATCH_FIELDS = (
    "variable_name",
    "point_groups",
    "address",
    "data_type",
    "scale",
    "offset",
    "unit",
    "description",
)


@dataclass(frozen=True, slots=True)
class PointPatch:
    """点表继承中的点补丁；``declared`` 记录 YAML 中实际写出的字段名。"""

    point_id: str
    declared: frozenset[str]
    variable_name: str | None = None
    point_groups: tuple[str, ...] | None = None
    address: Mapping[str, Any] | None = None
    data_type: str | None = None
    scale: float | None = None
    offset: float | None = None
    unit: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class PointTableDraft:
    """未展开的点表继承声明（解析输入，不是配置 VO）。"""

    protocol: str | None = None
    extends: str | None = None
    remove_points: tuple[str, ...] = ()
    points: tuple[PointPatch, ...] = ()


def resolve_point_tables(
    tables: Mapping[str, PointTableDraft],
) -> dict[str, PointTableConfig]:
    """解析全部点表草稿，返回继承展开后的 ``{表名: PointTableConfig}``。

    Raises:
        ConfigError: 父表不存在、继承环、protocol 规则违反、
            ``remove_points`` 引用未知点、或 merge/新建结果不构成合法完整点。
    """
    cache: dict[str, PointTableConfig] = {}
    for name in tables:
        _resolve_table(name, tables, [], cache)
    return cache


def _resolve_table(
    name: str,
    tables: Mapping[str, PointTableDraft],
    stack: list[str],
    cache: dict[str, PointTableConfig],
) -> PointTableConfig:
    """解析单张表（保持点声明顺序），结果缓存。"""
    if name in cache:
        return cache[name]
    if name in stack:
        cycle = " -> ".join([*stack, name])
        raise ConfigError(f"Point table inheritance cycle detected: {cycle}")
    table = tables.get(name)
    if table is None:
        parent = stack[-1] if stack else "<unknown>"
        raise ConfigError(f"Point table '{parent}' extends unknown table '{name}'")

    points: dict[str, PointConfig] = {}
    parent_protocol: str | None = None
    if table.extends is not None:
        base = _resolve_table(table.extends, tables, [*stack, name], cache)
        parent_protocol = base.protocol
        points = dict(base.points)

    protocol = _resolve_protocol(name, table, parent_protocol)

    for point_id in table.remove_points:
        if point_id not in points:
            raise ConfigError(
                f"Point table '{name}': remove_points references unknown point "
                f"'{point_id}' (not present in resolved parent)"
            )
        del points[point_id]

    for patch in table.points:
        if patch.point_id in points:
            points[patch.point_id] = _merge_point(points[patch.point_id], patch, name)
        else:
            points[patch.point_id] = _create_point(patch, name)

    resolved = PointTableConfig(protocol=protocol, points=points)
    cache[name] = resolved
    return resolved


def _resolve_protocol(
    name: str,
    table: PointTableDraft,
    parent_protocol: str | None,
) -> str:
    """基础表 protocol 必填；子表缺省继承且禁止跨协议继承。"""
    if parent_protocol is None:
        if table.protocol is None:
            raise ConfigError(
                f"Point table '{name}': protocol is required on a base table "
                "(one of ads/modbus/iec104)"
            )
        return table.protocol
    if table.protocol is not None and table.protocol != parent_protocol:
        raise ConfigError(
            f"Point table '{name}': protocol '{table.protocol}' does not match "
            f"parent table '{table.extends}' protocol '{parent_protocol}' "
            "(cross-protocol inheritance is not allowed)"
        )
    return parent_protocol


def _merge_point(base: PointConfig, patch: PointPatch, table: str) -> PointConfig:
    """override：以 patch 中实际写出的字段覆盖父表点。"""
    data: dict[str, Any] = {
        "point_id": base.point_id,
        "variable_name": base.variable_name,
        "point_groups": base.point_groups,
        "address": base.address,
        "data_type": base.data_type,
        "scale": base.scale,
        "offset": base.offset,
        "unit": base.unit,
        "description": base.description,
    }
    for name in _PATCH_FIELDS:
        if name in patch.declared:
            data[name] = getattr(patch, name)
    return _build_point(data, table)


def _create_point(patch: PointPatch, table: str) -> PointConfig:
    """append：patch 的 point_id 在父表结果中不存在——由补丁新建完整点。"""
    data: dict[str, Any] = {"point_id": patch.point_id}
    for name in _PATCH_FIELDS:
        if name in patch.declared:
            data[name] = getattr(patch, name)
    return _build_point(data, table)


#: 字段默认值——仅在字段完全未声明（既未继承也未写出）时应用；
#: 显式 null 与未声明不同：不可空字段收到显式 null 是配置错误（旧语义）。
_DEFAULTS: dict[str, Any] = {
    "variable_name": None,
    "data_type": "float32",
    "scale": 1.0,
    "offset": 0.0,
    "unit": "none",
    "description": None,
}

_NULLABLE_FIELDS = frozenset({"variable_name", "description"})


def _build_point(data: dict[str, Any], table: str) -> PointConfig:
    """把 merge/新建结果构造为完整 PointConfig（默认值与旧契约一致）。"""
    point_id = data.get("point_id")
    if not isinstance(point_id, str):
        raise ConfigError(f"Point table '{table}': resolved point missing 'point_id'")
    context = f"Point table '{table}': resolved point '{point_id}'"
    values: dict[str, Any] = {}
    for name in _PATCH_FIELDS:
        if name in data:
            value = data[name]
            if value is None and name not in _NULLABLE_FIELDS:
                raise ConfigError(f"{context}: field '{name}' must not be null")
            values[name] = value
        elif name in _DEFAULTS:
            values[name] = _DEFAULTS[name]
        else:
            raise ConfigError(f"{context}: missing required field '{name}'")
    try:
        return PointConfig(
            point_id=point_id,
            variable_name=values["variable_name"],
            point_groups=tuple(values["point_groups"]),
            address=freeze_config_value(values["address"]),
            data_type=values["data_type"],
            scale=values["scale"],
            offset=values["offset"],
            unit=values["unit"],
            description=values["description"],
        )
    except ValueError as exc:
        raise ConfigError(f"{context} is invalid: {exc}") from exc


__all__ = ["PointPatch", "PointTableDraft", "resolve_point_tables"]
