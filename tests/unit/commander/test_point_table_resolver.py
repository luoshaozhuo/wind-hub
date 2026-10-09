"""新 Commander 点表继承解析单元测试。"""

from __future__ import annotations

from typing import Any

import pytest

from core.application import ConfigError
from core.infrastructure.config.point_tables import (
    PointPatch,
    PointTableDraft,
    resolve_point_tables,
)


def _patch(data: dict[str, Any]) -> PointPatch:
    declared = frozenset(set(data) - {"point_id"})
    return PointPatch(
        point_id=data["point_id"],
        declared=declared,
        variable_name=data.get("variable_name") if "variable_name" in data else None,
        point_groups=(
            tuple(data["point_groups"]) if "point_groups" in data else None
        ),
        address=data.get("address") if "address" in data else None,
        data_type=data.get("data_type") if "data_type" in data else None,
        scale=data.get("scale") if "scale" in data else None,
        offset=data.get("offset") if "offset" in data else None,
        unit=data.get("unit") if "unit" in data else None,
        description=data.get("description") if "description" in data else None,
    )


def _table(data: dict[str, Any]) -> PointTableDraft:
    return PointTableDraft(
        protocol=data.get("protocol"),
        extends=data.get("extends"),
        remove_points=tuple(data.get("remove_points", ())),
        points=tuple(_patch(item) for item in data.get("points", ())),
    )


def test_base_table_requires_protocol():
    with pytest.raises(ConfigError, match="protocol is required"):
        resolve_point_tables({"tab": _table({"points": []})})


def test_inheritance_override_and_append():
    tables = {
        "base": _table(
            {
                "protocol": "modbus",
                "points": [
                    {
                        "point_id": "p1",
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 1},
                        "data_type": "float32",
                        "scale": 1.0,
                    },
                    {
                        "point_id": "p2",
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 2},
                        "data_type": "float32",
                    },
                ],
            }
        ),
        "child": _table(
            {
                "extends": "base",
                "remove_points": ["p2"],
                "points": [
                    {
                        "point_id": "p1",
                        "scale": 0.1,
                    },
                    {
                        "point_id": "p3",
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 3},
                        "data_type": "int16",
                    },
                ],
            }
        ),
    }
    resolved = resolve_point_tables(tables)
    child = resolved["child"]
    assert child.protocol == "modbus"
    assert set(child.points) == {"p1", "p3"}
    assert child.points["p1"].scale == 0.1
    # 未写字段继承父值
    assert child.points["p1"].address["address"] == 1
    assert child.points["p1"].data_type == "float32"


def test_explicit_null_overrides_parent():
    tables = {
        "base": _table(
            {
                "protocol": "modbus",
                "points": [
                    {
                        "point_id": "p1",
                        "variable_name": "name",
                        "point_groups": ["g"],
                        "address": {"register_type": "holding", "address": 1},
                        "data_type": "float32",
                    }
                ],
            }
        ),
        "child": _table(
            {
                "extends": "base",
                "points": [{"point_id": "p1", "variable_name": None}],
            }
        ),
    }
    resolved = resolve_point_tables(tables)
    assert resolved["child"].points["p1"].variable_name is None


def test_cycle_detected():
    tables = {
        "a": _table({"extends": "b", "protocol": "modbus"}),
        "b": _table({"extends": "a"}),
    }
    with pytest.raises(ConfigError, match="cycle"):
        resolve_point_tables(tables)


def test_unknown_parent_rejected():
    tables = {"child": _table({"extends": "ghost"})}
    with pytest.raises(ConfigError, match="unknown table"):
        resolve_point_tables(tables)


def test_remove_unknown_point_rejected():
    tables = {
        "base": _table({"protocol": "modbus"}),
        "child": _table({"extends": "base", "remove_points": ["nope"]}),
    }
    with pytest.raises(ConfigError, match="unknown point"):
        resolve_point_tables(tables)


def test_cross_protocol_inheritance_rejected():
    tables = {
        "base": _table({"protocol": "modbus"}),
        "child": _table({"extends": "base", "protocol": "ads"}),
    }
    with pytest.raises(ConfigError, match="cross-protocol"):
        resolve_point_tables(tables)


def test_incomplete_new_point_rejected():
    tables = {
        "base": _table({"protocol": "modbus"}),
        "child": _table(
            {
                "extends": "base",
                "points": [{"point_id": "p1", "point_groups": ["g"]}],
            }
        ),
    }
    with pytest.raises(ConfigError, match="missing required field"):
        resolve_point_tables(tables)
