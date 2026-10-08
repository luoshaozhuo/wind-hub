"""Point Table 单继承解析——Raw 点表 → 继承展开后的完整点集。

解析流程（对每张表，带 cache 与 cycle 检测）::

    resolve_table(name, stack):
        cache 命中 → 直接返回
        name 已在 stack → ConfigError（cycle path）
        extends → 递归解析父表并拷贝其结果
        protocol → 基础表必填；子表缺省继承父表，显式配置必须与父表一致
        remove_points → 逐个点校验存在后删除
        points → point_id 已存在则 merge（override），否则新建（append）
        全部结果经 PointConfigRaw.model_validate 完整校验

merge 语义与旧系统一致：以 ``model_fields_set`` 区分「未写」（继承父值）
与「写了（含显式 null）」（覆盖父值）；``address`` 为整体覆盖。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from core.application import ConfigError

from .raw import PointConfigRaw, PointPatchRaw, PointTableRaw


class ResolvedTable:
    """单张表的继承展开结果——最终 protocol + 有序完整点集。"""

    __slots__ = ("protocol", "points")

    def __init__(self, protocol: str, points: dict[str, PointConfigRaw]) -> None:
        self.protocol = protocol
        self.points: Mapping[str, PointConfigRaw] = MappingProxyType(dict(points))


def resolve_point_tables(
    tables: dict[str, PointTableRaw],
) -> dict[str, ResolvedTable]:
    """解析全部 Raw 点表，返回继承展开后的 ``{表名: ResolvedTable}``。

    Raises:
        ConfigError: 父表不存在、继承环、protocol 规则违反、
            ``remove_points`` 引用未知点、或 merge/新建结果不构成合法完整点。
    """
    cache: dict[str, ResolvedTable] = {}
    for name in tables:
        _resolve_table(name, tables, [], cache)
    return cache


def _resolve_table(
    name: str,
    tables: dict[str, PointTableRaw],
    stack: list[str],
    cache: dict[str, ResolvedTable],
) -> ResolvedTable:
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

    points: dict[str, PointConfigRaw] = {}
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

    resolved = ResolvedTable(protocol, points)
    cache[name] = resolved
    return resolved


def _resolve_protocol(
    name: str,
    table: PointTableRaw,
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


def _merge_point(
    base: PointConfigRaw,
    patch: PointPatchRaw,
    table: str,
) -> PointConfigRaw:
    """override：以 patch 中实际写出的字段覆盖父表点。"""
    data = base.model_dump()
    for field in patch.model_fields_set:
        if field == "point_id":
            continue
        data[field] = getattr(patch, field)
    return _validate_resolved_point(data, table)


def _create_point(patch: PointPatchRaw, table: str) -> PointConfigRaw:
    """append：patch 的 point_id 在父表结果中不存在——由补丁新建完整点。"""
    data = {field: getattr(patch, field) for field in patch.model_fields_set}
    return _validate_resolved_point(data, table)


def _validate_resolved_point(data: dict[str, Any], table: str) -> PointConfigRaw:
    """把 merge/新建结果校验为完整 PointConfigRaw。"""
    try:
        return PointConfigRaw.model_validate(data)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"Point table '{table}': resolved point " f"'{data.get('point_id')}' is invalid: {exc}"
        ) from exc


__all__ = ["ResolvedTable", "resolve_point_tables"]
