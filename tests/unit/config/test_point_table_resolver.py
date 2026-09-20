"""Point Table 继承解析（``config/point_table_resolver.py``）的单元测试。

验证对象：Raw 点表（``PointTableConfig`` + ``PointPatch``）经
``resolve_point_tables`` 展开为完整 ``PointConfig`` 集的全部规则——
单/多级继承、字段 merge（含「未写 vs 显式 null」区分）、address/sinks
整体替换、remove_points、extends 错误与继承后统一校验。
"""

from __future__ import annotations

import pytest

from wind_hub.config.point_table_resolver import resolve_point_tables
from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import (
    PointAddress,
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
)
from wind_hub.domain.model.errors import ConfigError
from wind_hub.domain.model.route import RouteMatch, RouteRule, RouteTarget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_patch(
    point_id: str,
    group: str = "fast",
    symbol: str = "MAIN.x",
    **fields: object,
) -> PointPatch:
    """构造信息完整的补丁（基础表的点即「全写字段」的补丁）。"""
    return PointPatch(
        point_id=point_id,
        group=group,
        address=PointAddress(symbol=symbol),
        data_type="float32",
        **fields,  # type: ignore[arg-type]
    )


def _raw(tables: dict[str, PointTableConfig]) -> PointTablesConfig:
    return PointTablesConfig(tables=tables)


def _resolve_points(tables: dict[str, PointTableConfig], name: str) -> dict[str, PointConfig]:
    resolved = resolve_point_tables(_raw(tables))
    return {p.point_id: p for p in resolved.tables[name].points}


# ---------------------------------------------------------------------------
# 继承与字段 merge
# ---------------------------------------------------------------------------


class TestInheritance:
    def test_single_level_inheritance(self) -> None:
        """单层继承：子表不做任何修改，结果与父表一致。"""
        tables = {
            "base": PointTableConfig(
                points=[
                    _full_patch("p001", unit="rpm"),
                    _full_patch("p002", group="slow", unit="kW"),
                ]
            ),
            "child": PointTableConfig(extends="base"),
        }
        points = _resolve_points(tables, "child")
        assert set(points) == {"p001", "p002"}
        assert points["p001"].unit == "rpm"
        assert points["p002"].group == "slow"

    def test_multi_level_inheritance(self) -> None:
        """多级继承：A → B → C，基础表字段穿透到最底层。"""
        tables = {
            "a": PointTableConfig(points=[_full_patch("p001", unit="rpm")]),
            "b": PointTableConfig(
                extends="a", points=[PointPatch(point_id="p001", max_value=2000.0)]
            ),
            "c": PointTableConfig(extends="b", points=[PointPatch(point_id="p001", group="slow")]),
        }
        points = _resolve_points(tables, "c")
        assert points["p001"].unit == "rpm"  # 来自 a
        assert points["p001"].max_value == 2000.0  # 来自 b
        assert points["p001"].group == "slow"  # 来自 c

    def test_shared_parent_resolved_once_for_multiple_children(self) -> None:
        """多张子表共享同一父表：各自独立获得父表点集。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p001")]),
            "child1": PointTableConfig(extends="base"),
            "child2": PointTableConfig(extends="base"),
        }
        resolved = resolve_point_tables(_raw(tables))
        assert [p.point_id for p in resolved.tables["child1"].points] == ["p001"]
        assert [p.point_id for p in resolved.tables["child2"].points] == ["p001"]

    def test_field_override_keeps_other_fields(self) -> None:
        """普通字段 override：只覆盖写出的字段，其余继承。"""
        tables = {
            "base": PointTableConfig(
                points=[
                    _full_patch("p002", variable_name="gen_power", unit="kW", max_value=10000.0)
                ]
            ),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p002", max_value=2500.0)]
            ),
        }
        points = _resolve_points(tables, "child")
        assert points["p002"].max_value == 2500.0  # 覆盖
        assert points["p002"].unit == "kW"  # 继承
        assert points["p002"].variable_name == "gen_power"  # 继承

    def test_unwritten_nullable_field_inherited(self) -> None:
        """可空字段未写 → 继承父表值（与显式 null 区分）。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p002", unit="kW")]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p002", max_value=2500.0)]
            ),
        }
        assert _resolve_points(tables, "child")["p002"].unit == "kW"

    def test_explicit_null_clears_nullable_field(self) -> None:
        """显式写 ``unit: null`` → 覆盖为 None，而非继承。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p002", unit="kW")]),
            "child": PointTableConfig(
                extends="base",
                # 显式传 None——进入 model_fields_set，语义等同 YAML `unit: null`
                points=[PointPatch(point_id="p002", unit=None)],
            ),
        }
        assert _resolve_points(tables, "child")["p002"].unit is None

    def test_address_replaced_as_a_whole(self) -> None:
        """address 整体替换：子表只写 symbol，父表 index 字段不得残留。"""
        tables = {
            "base": PointTableConfig(
                points=[
                    _full_patch("p003").model_copy(
                        update={
                            "address": PointAddress(
                                symbol="MAIN.oldValue", index_group=0x4020, index_offset=100
                            )
                        }
                    )
                ]
            ),
            "child": PointTableConfig(
                extends="base",
                points=[PointPatch(point_id="p003", address=PointAddress(symbol="MAIN.newValue"))],
            ),
        }
        addr = _resolve_points(tables, "child")["p003"].address
        extra = addr.model_extra or {}
        assert extra.get("symbol") == "MAIN.newValue"
        assert "index_group" not in extra
        assert "index_offset" not in extra

    def test_sinks_replaced_as_a_whole(self) -> None:
        """sinks 整体替换：不 append；未写才继承。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p001", sinks=["kafka_main", "db_main"])]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p001", sinks=["file_archive"])]
            ),
            "inheriting": PointTableConfig(extends="base"),
        }
        assert _resolve_points(tables, "child")["p001"].sinks == ["file_archive"]
        assert _resolve_points(tables, "inheriting")["p001"].sinks == ["kafka_main", "db_main"]

    def test_append_new_point(self) -> None:
        """子表写了父表没有的 point_id → 新增点。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p001")]),
            "child": PointTableConfig(
                extends="base",
                points=[_full_patch("p099", group="slow", symbol="MAIN.value")],
            ),
        }
        points = _resolve_points(tables, "child")
        assert set(points) == {"p001", "p099"}
        assert points["p099"].group == "slow"

    def test_remove_points(self) -> None:
        tables = {
            "base": PointTableConfig(
                points=[_full_patch("p001"), _full_patch("p003", group="slow")]
            ),
            "child": PointTableConfig(extends="base", remove_points=["p003"]),
        }
        points = _resolve_points(tables, "child")
        assert set(points) == {"p001"}


# ---------------------------------------------------------------------------
# 错误校验
# ---------------------------------------------------------------------------


class TestResolveErrors:
    def test_remove_unknown_point_raises(self) -> None:
        tables = {
            "base": PointTableConfig(points=[_full_patch("p001")]),
            "child": PointTableConfig(extends="base", remove_points=["p999"]),
        }
        with pytest.raises(ConfigError, match="p999"):
            resolve_point_tables(_raw(tables))

    def test_duplicate_point_id_in_raw_table_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate"):
            PointTableConfig(points=[PointPatch(point_id="p1"), PointPatch(point_id="p1")])

    def test_duplicate_remove_points_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate"):
            PointTableConfig(extends="base", remove_points=["p1", "p1"])

    def test_extends_unknown_table_raises(self) -> None:
        tables = {"child": PointTableConfig(extends="unknown_table")}
        with pytest.raises(ConfigError, match="unknown table 'unknown_table'"):
            resolve_point_tables(_raw(tables))

    def test_self_inheritance_raises(self) -> None:
        tables = {"a": PointTableConfig(extends="a")}
        with pytest.raises(ConfigError, match="cycle"):
            resolve_point_tables(_raw(tables))

    def test_inheritance_cycle_raises_with_full_path(self) -> None:
        tables = {
            "A": PointTableConfig(extends="B"),
            "B": PointTableConfig(extends="C"),
            "C": PointTableConfig(extends="A"),
        }
        with pytest.raises(ConfigError, match=r"A -> B -> C -> A"):
            resolve_point_tables(_raw(tables))

    def test_new_point_missing_required_fields_raises(self) -> None:
        """新增点信息不完整（缺 address）→ resolve 阶段配置错误。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p001")]),
            "child": PointTableConfig(extends="base", points=[PointPatch(point_id="p099")]),
        }
        with pytest.raises(ConfigError, match="p099"):
            resolve_point_tables(_raw(tables))

    def test_override_resulting_in_invalid_point_raises(self) -> None:
        """override 后完整点非法（min_value >= max_value）→ 配置错误。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p001", max_value=5.0)]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p001", min_value=10.0)]
            ),
        }
        with pytest.raises(ConfigError, match="min_value"):
            resolve_point_tables(_raw(tables))

    def test_invalid_data_type_rejected_after_resolve(self) -> None:
        """data_type 白名单在 resolved 阶段校验（Raw Patch 阶段不查）。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p001")]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p001", data_type="imaginary")]
            ),
        }
        with pytest.raises(ConfigError, match="data_type"):
            resolve_point_tables(_raw(tables))


# ---------------------------------------------------------------------------
# resolved 结果驱动下游语义
# ---------------------------------------------------------------------------


class TestResolvedSemantics:
    def test_parent_change_changes_child_result(self) -> None:
        """父表内容变化 → 子表最终解析结果随之变化。"""
        child = PointTableConfig(extends="base")
        first = _resolve_points(
            {"base": PointTableConfig(points=[_full_patch("p001", unit="rpm")]), "child": child},
            "child",
        )
        second = _resolve_points(
            {"base": PointTableConfig(points=[_full_patch("p001", unit="rps")]), "child": child},
            "child",
        )
        assert first["p001"].unit == "rpm"
        assert second["p001"].unit == "rps"

    def test_routing_uses_resolved_point_group(self) -> None:
        """继承后 group 改变 → RoutingTable 按最终 point_group 匹配。"""
        tables = {
            "base": PointTableConfig(points=[_full_patch("p003", group="slow")]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p003", group="fast")]
            ),
        }
        resolved = resolve_point_tables(_raw(tables))
        rules = [
            RouteRule(
                name="fast-only",
                match=RouteMatch(point_group="fast"),
                targets=[RouteTarget(sink="kafka")],
            )
        ]
        table = RoutingTable(rules, {"d1": resolved.tables["child"].points})
        assert table.resolve("d1", "p003") == ["kafka"]
