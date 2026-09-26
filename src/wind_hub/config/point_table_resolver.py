"""Point Table 继承解析——Raw PointTables → Resolved PointTables。

职责边界：本模块只负责把 ``points.yaml`` 的 Raw 点表（
:class:`~wind_hub.config.schema.PointTableConfig`，含 ``extends`` /
``remove_points`` / :class:`~wind_hub.config.schema.PointPatch`）展开为
完整 :class:`~wind_hub.config.schema.PointConfig` 集。设备绑定交叉校验、
路由编译、运行时注入都在本模块之外，且只接触 resolved 结果。

解析流程（对每张表，带 cache 与 cycle 检测）::

    resolve_table(name, stack):
        cache 命中 → 直接返回
        name 已在 stack → ConfigError（cycle path）
        extends → 递归解析父表并拷贝其结果
        protocol → 基础表必填；子表缺省继承父表，显式配置必须与父表一致
        remove_points → 逐个点校验存在后删除
        points → point_id 已存在则 merge（override），否则新建（append）
        全部结果经 PointConfig.model_validate 完整校验
"""

from __future__ import annotations

from typing import Any

from wind_hub.config.schema import (
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
    ResolvedPointTable,
    ResolvedPointTables,
)
from wind_hub.domain.model.errors import ConfigError


def resolve_point_tables(raw: PointTablesConfig) -> ResolvedPointTables:
    """解析全部 Raw 点表，返回继承展开后的完整点表集。

    每张表独立解析并缓存——多张子表共享同一父表时父表只解析一次。
    解析顺序：父表结果 → ``remove_points`` → 本表 points override/append。

    Raises:
        ConfigError: 父表不存在、继承环、protocol 规则违反（基础表缺失 /
            子表与父表不一致）、``remove_points`` 引用未知点、或
            merge/新建结果不构成合法完整点。
    """
    cache: dict[str, _ResolvedTable] = {}
    for name in raw.tables:
        _resolve_table(name, raw.tables, [], cache)
    return ResolvedPointTables(
        tables={
            name: ResolvedPointTable(protocol=t.protocol, points=list(t.points.values()))
            for name, t in cache.items()
        }
    )


class _ResolvedTable:
    """单张表的中间解析结果——最终 protocol + 有序点集。"""

    __slots__ = ("protocol", "points")

    def __init__(self, protocol: str, points: dict[str, PointConfig]) -> None:
        self.protocol = protocol
        self.points = points


def _resolve_table(
    name: str,
    tables: dict[str, PointTableConfig],
    stack: list[str],
    cache: dict[str, _ResolvedTable],
) -> _ResolvedTable:
    """解析单张表（保持点声明顺序），结果缓存。"""
    if name in cache:
        return cache[name]
    if name in stack:
        cycle = " -> ".join([*stack, name])
        raise ConfigError(f"Point table inheritance cycle detected: {cycle}")
    table = tables.get(name)
    if table is None:
        # 只可能由 extends 引用触发——顶层表名遍历自 tables 自身
        parent = stack[-1] if stack else "<unknown>"
        raise ConfigError(f"Point table '{parent}' extends unknown table '{name}'")

    points: dict[str, PointConfig] = {}
    parent_protocol: str | None = None
    if table.extends is not None:
        base = _resolve_table(table.extends, tables, [*stack, name], cache)
        parent_protocol = base.protocol
        # PointConfig 加载后即不可变快照——拷贝 dict 结构即可，不逐点深拷
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

    resolved = _ResolvedTable(protocol, points)
    cache[name] = resolved
    return resolved


def _resolve_protocol(
    name: str, table: PointTableConfig, parent_protocol: str | None
) -> str:
    """解析点表的最终 protocol。

    - 基础表（无 ``extends``）：``protocol`` 必填；
    - 子表：缺省继承父表；显式配置时必须与父表一致（禁止跨协议继承）。
    """
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
    """override：以 patch 中**实际写出**的字段覆盖父表点。

    以 ``model_fields_set`` 区分「未写」与「显式 null」：未写继承父值；
    写了（含 null）覆盖父值。``address`` 与 ``sinks`` 与其他字段同为整体
    覆盖——显式配置即全量替换父值，不做递归 merge / append。
    """
    data = base.model_dump()
    for field in patch.model_fields_set:
        if field == "point_id":
            continue
        data[field] = getattr(patch, field)
    return _validate_resolved_point(data, table)


def _create_point(patch: PointPatch, table: str) -> PointConfig:
    """append：patch 的 point_id 在父表结果中不存在——由补丁新建完整点。

    缺少构成完整 :class:`PointConfig` 所必须的信息（如 ``address``）时
    在本阶段报配置错误。
    """
    data = {field: getattr(patch, field) for field in patch.model_fields_set}
    return _validate_resolved_point(data, table)


def _validate_resolved_point(data: dict[str, Any], table: str) -> PointConfig:
    """把 merge/新建结果校验为完整 PointConfig（继承后统一校验入口）。

    PointConfig 自身的业务约束（point_groups 等）抛 ConfigError，
    原样上抛；结构性错误（缺 address、类型不符等）包装为带表名与点
    上下文的 ConfigError。
    """
    try:
        return PointConfig.model_validate(data)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"Point table '{table}': resolved point " f"'{data.get('point_id')}' is invalid: {exc}"
        ) from exc
