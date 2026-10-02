"""Point Table 继承解析（``config/point_table_resolver.py``）的单元测试。

验证对象：Raw 点表（``PointTableConfig`` + ``PointPatch``）经
``resolve_point_tables`` 展开为完整 ``PointConfig`` 集的全部规则——
单/多级继承、字段 merge（含「未写 vs 显式 null」区分）、address /
point_groups 整体替换、remove_points、extends 错误与继承后统一校验
（含 merge 后 point_groups 非空/去重/非空白）。
"""

from __future__ import annotations

import pytest

from wind_hub_core.config.point_table_resolver import resolve_point_tables
from wind_hub_core.config.schema import (
    PointAddress,
    PointConfig,
    PointPatch,
    PointTableConfig,
    PointTablesConfig,
)
from wind_hub_core.model.errors import ConfigError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_patch(
    point_id: str,
    point_groups: list[str] | None = None,
    symbol: str = "MAIN.x",
    **fields: object,
) -> PointPatch:
    """构造信息完整的补丁（基础表的点即「全写字段」的补丁）。"""
    return PointPatch(
        point_id=point_id,
        point_groups=point_groups or ["fast"],
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
                protocol="ads",
                points=[
                    _full_patch("p001", unit="rpm"),
                    _full_patch("p002", point_groups=["slow"], unit="kW"),
                ]
            ),
            "child": PointTableConfig(extends="base"),
        }
        points = _resolve_points(tables, "child")
        assert set(points) == {"p001", "p002"}
        assert points["p001"].unit == "rpm"
        assert points["p002"].point_groups == ["slow"]

    def test_multi_level_inheritance(self) -> None:
        """多级继承：A → B → C，基础表字段穿透到最底层。"""
        tables = {
            "a": PointTableConfig(protocol="ads", points=[_full_patch("p001", unit="rpm")]),
            "b": PointTableConfig(extends="a", points=[PointPatch(point_id="p001", scale=2000.0)]),
            "c": PointTableConfig(
                extends="b", points=[PointPatch(point_id="p001", point_groups=["slow"])]
            ),
        }
        points = _resolve_points(tables, "c")
        assert points["p001"].unit == "rpm"  # 来自 a
        assert points["p001"].scale == 2000.0  # 来自 b
        assert points["p001"].point_groups == ["slow"]  # 来自 c

    def test_shared_parent_resolved_once_for_multiple_children(self) -> None:
        """多张子表共享同一父表：各自独立获得父表点集。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
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
                protocol="ads",
                points=[_full_patch("p002", variable_name="gen_power", unit="kW", scale=10000.0)]
            ),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p002", scale=2500.0)]
            ),
        }
        points = _resolve_points(tables, "child")
        assert points["p002"].scale == 2500.0  # 覆盖
        assert points["p002"].unit == "kW"  # 继承
        assert points["p002"].variable_name == "gen_power"  # 继承

    def test_unwritten_nullable_field_inherited(self) -> None:
        """可空字段未写 → 继承父表值（与显式 null 区分）。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p002", unit="kW")]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p002", scale=2500.0)]
            ),
        }
        assert _resolve_points(tables, "child")["p002"].unit == "kW"

    def test_explicit_null_unit_rejected(self) -> None:
        """``unit`` 是非空 unit ID（缺省 ``'none'``）——显式写 ``unit: null``
        覆盖出非法 resolved 点，是配置错误。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p002", unit="kW")]),
            "child": PointTableConfig(
                extends="base",
                # 显式传 None——进入 model_fields_set，语义等同 YAML `unit: null`
                points=[PointPatch(point_id="p002", unit=None)],
            ),
        }
        with pytest.raises(ConfigError, match="p002"):
            resolve_point_tables(_raw(tables))

    def test_address_replaced_as_a_whole(self) -> None:
        """address 整体替换：子表只写 symbol，父表 index 字段不得残留。"""
        tables = {
            "base": PointTableConfig(
                protocol="ads",
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

    def test_point_groups_replaced_as_a_whole(self) -> None:
        """point_groups 整体替换：不 append；未写才继承。"""
        tables = {
            "base": PointTableConfig(
                protocol="ads",
                points=[_full_patch("p001", point_groups=["fast", "telemetry"])]
            ),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p001", point_groups=["slow"])]
            ),
            "inheriting": PointTableConfig(extends="base"),
        }
        assert _resolve_points(tables, "child")["p001"].point_groups == ["slow"]
        assert _resolve_points(tables, "inheriting")["p001"].point_groups == ["fast", "telemetry"]

    def test_append_new_point(self) -> None:
        """子表写了父表没有的 point_id → 新增点。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(
                extends="base",
                points=[_full_patch("p099", point_groups=["slow"], symbol="MAIN.value")],
            ),
        }
        points = _resolve_points(tables, "child")
        assert set(points) == {"p001", "p099"}
        assert points["p099"].point_groups == ["slow"]

    def test_remove_points(self) -> None:
        tables = {
            "base": PointTableConfig(
                protocol="ads",
                points=[_full_patch("p001"), _full_patch("p003", point_groups=["slow"])]
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
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(extends="base", remove_points=["p999"]),
        }
        with pytest.raises(ConfigError, match="p999"):
            resolve_point_tables(_raw(tables))

    def test_duplicate_point_id_in_raw_table_raises(self) -> None:
        with pytest.raises(ConfigError, match="Duplicate"):
            PointTableConfig(protocol="ads", points=[PointPatch(point_id="p1"), PointPatch(point_id="p1")])

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
        """新增点信息不完整（缺 address / point_groups）→ resolve 阶段配置错误。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(extends="base", points=[PointPatch(point_id="p099")]),
        }
        with pytest.raises(ConfigError, match="p099"):
            resolve_point_tables(_raw(tables))

    def test_new_point_missing_point_groups_raises(self) -> None:
        """新增点缺 point_groups（必填）→ 配置错误。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(
                extends="base",
                points=[
                    PointPatch(
                        point_id="p099",
                        address=PointAddress(symbol="MAIN.v"),
                        data_type="float32",
                    )
                ],
            ),
        }
        with pytest.raises(ConfigError, match="p099"):
            resolve_point_tables(_raw(tables))

    def test_override_resulting_in_invalid_point_raises(self) -> None:
        """override 后完整点非法（point_groups 置空）→ 配置错误。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p001", point_groups=[])]
            ),
        }
        with pytest.raises(ConfigError, match="point_groups"):
            resolve_point_tables(_raw(tables))

    def test_invalid_data_type_rejected_after_resolve(self) -> None:
        """data_type 白名单在 resolved 阶段校验（Raw Patch 阶段不查）。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p001", data_type="imaginary")]
            ),
        }
        with pytest.raises(ConfigError, match="data_type"):
            resolve_point_tables(_raw(tables))


class TestMergedPointGroupsValidation:
    """point_groups 的非空/去重/非空白校验在继承 merge 后同样执行。"""

    def _tables_with_child_groups(self, groups: list[str]) -> dict[str, PointTableConfig]:
        return {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001", point_groups=["fast"])]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p001", point_groups=groups)]
            ),
        }

    def test_merged_point_groups_empty_list_raises(self) -> None:
        with pytest.raises(ConfigError, match="non-empty"):
            resolve_point_tables(_raw(self._tables_with_child_groups([])))

    def test_merged_point_groups_duplicates_raise(self) -> None:
        with pytest.raises(ConfigError, match="duplicate point_groups"):
            resolve_point_tables(_raw(self._tables_with_child_groups(["slow", "slow"])))

    def test_merged_point_groups_blank_string_raises(self) -> None:
        with pytest.raises(ConfigError, match="non-empty strings"):
            resolve_point_tables(_raw(self._tables_with_child_groups(["  "])))

    def test_base_table_point_groups_validated(self) -> None:
        """基础表的点（无继承 merge）同样在 resolved 阶段完整校验。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001", point_groups=["fast", "fast"])]),
        }
        with pytest.raises(ConfigError, match="duplicate point_groups"):
            resolve_point_tables(_raw(tables))


# ---------------------------------------------------------------------------
# resolved 结果驱动下游语义
# ---------------------------------------------------------------------------


class TestResolvedSemantics:
    def test_parent_change_changes_child_result(self) -> None:
        """父表内容变化 → 子表最终解析结果随之变化。"""
        child = PointTableConfig(extends="base")
        first = _resolve_points(
            {"base": PointTableConfig(protocol="ads", points=[_full_patch("p001", unit="rpm")]), "child": child},
            "child",
        )
        second = _resolve_points(
            {"base": PointTableConfig(protocol="ads", points=[_full_patch("p001", unit="rps")]), "child": child},
            "child",
        )
        assert first["p001"].unit == "rpm"
        assert second["p001"].unit == "rps"

    def test_task_point_selection_uses_resolved_point_groups(self) -> None:
        """Task 选点（``point_group in point.point_groups``）按继承后的最终分组。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p003", point_groups=["slow"])]),
            "child": PointTableConfig(
                extends="base", points=[PointPatch(point_id="p003", point_groups=["fast"])]
            ),
        }
        resolved = resolve_point_tables(_raw(tables))
        points = resolved.tables["child"].points
        selected = [p.point_id for p in points if "fast" in p.point_groups]
        assert selected == ["p003"]
        assert all("slow" not in p.point_groups for p in points)


# ---------------------------------------------------------------------------
# protocol 继承规则
# ---------------------------------------------------------------------------


class TestProtocolResolution:
    def test_base_table_requires_protocol(self) -> None:
        """基础表（无 extends）缺 protocol → 配置错误。"""
        tables = {"base": PointTableConfig()}
        with pytest.raises(ConfigError, match="protocol is required"):
            resolve_point_tables(_raw(tables))

    def test_child_inherits_parent_protocol(self) -> None:
        """子表缺省继承父表 protocol，并进入 resolved 结果。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(extends="base"),
        }
        resolved = resolve_point_tables(_raw(tables))
        assert resolved.tables["base"].protocol == "ads"
        assert resolved.tables["child"].protocol == "ads"

    def test_child_explicit_same_protocol_accepted(self) -> None:
        """子表显式写与父表一致的 protocol：允许。"""
        tables = {
            "base": PointTableConfig(protocol="modbus", points=[_full_patch("p001")]),
            "child": PointTableConfig(extends="base", protocol="modbus"),
        }
        assert resolve_point_tables(_raw(tables)).tables["child"].protocol == "modbus"

    def test_cross_protocol_inheritance_rejected(self) -> None:
        """子表显式写与父表不同的 protocol → 配置错误（禁止跨协议继承）。"""
        tables = {
            "base": PointTableConfig(protocol="ads", points=[_full_patch("p001")]),
            "child": PointTableConfig(extends="base", protocol="modbus"),
        }
        with pytest.raises(ConfigError, match="cross-protocol"):
            resolve_point_tables(_raw(tables))

    def test_invalid_protocol_rejected_by_schema(self) -> None:
        """protocol 必须在 SUPPORTED_PROTOCOLS 内（schema 层校验）。"""
        with pytest.raises(ConfigError, match="protocol"):
            PointTableConfig(protocol="opcua")
