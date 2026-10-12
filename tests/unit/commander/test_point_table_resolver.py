"""Core 点表继承解析回归测试，通过实际 Commander 配置加载入口验证。"""

from __future__ import annotations

from typing import Any

import pytest

from commander.infrastructure.config import load_commander_config
from core.application import ConfigError
from core.domain import PointTableId
from core.infrastructure.config import YamlConfigAdapter
from tests.support.new_commander import write_minimal_config_tree


def _base_point(address: int, *, data_type: str = "float32") -> dict[str, Any]:
    return {
        "point_groups": ["g"],
        "address": {"register_type": "holding", "address": address},
        "data_type": data_type,
    }


def _load(tmp_path, tables: dict[str, Any]):
    directory = write_minimal_config_tree(
        tmp_path,
        point_tables=tables,
    )
    return load_commander_config(YamlConfigAdapter(directory))


def test_base_table_requires_protocol(tmp_path):
    with pytest.raises(ConfigError, match="protocol"):
        _load(tmp_path, {"tab": {"points": {}}})


def test_inheritance_override_append_and_remove(tmp_path):
    base = {
        "protocol": "modbus",
        "points": {"p1": _base_point(1), "p2": _base_point(2)},
    }
    child = {
        "parent_id": "tab",
        "remove_points": ["p2"],
        "points": {
            "p1": {"scale": 0.1},
            "p3": _base_point(3, data_type="int16"),
        },
    }
    config = _load(tmp_path, {"tab": base, "child": child})
    table = config.point_tables[PointTableId("child")]
    assert table.parent_id == PointTableId("tab")
    assert set(table.points) == {"p1", "p3"}
    assert table.points["p1"].scale == 0.1
    assert table.points["p1"].ext["address"] == 1


def test_explicit_null_clears_inherited_optional_name(tmp_path):
    base_point = {**_base_point(1), "variable_name": "original"}
    config = _load(
        tmp_path,
        {
            "tab": {"protocol": "modbus", "points": {"p1": base_point}},
            "child": {"parent_id": "tab", "points": {"p1": {"variable_name": None}}},
        },
    )
    assert config.point_tables[PointTableId("child")].points["p1"].variable_name is None


def test_inheritance_cycle_rejected(tmp_path):
    with pytest.raises(ConfigError, match="cycle"):
        _load(
            tmp_path,
            {
                "tab": {"parent_id": "child", "points": {}},
                "child": {"parent_id": "tab", "points": {}},
            },
        )


def test_unknown_parent_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        _load(tmp_path, {"tab": {"parent_id": "missing", "points": {}}})


def test_remove_unknown_point_rejected(tmp_path):
    with pytest.raises(ConfigError, match="cannot remove unknown point"):
        _load(
            tmp_path,
            {
                "tab": {"protocol": "modbus", "points": {}},
                "child": {"parent_id": "tab", "remove_points": ["missing"]},
            },
        )


def test_cross_protocol_inheritance_rejected(tmp_path):
    with pytest.raises(ConfigError, match="protocol differs"):
        _load(
            tmp_path,
            {
                "tab": {"protocol": "modbus", "points": {}},
                "child": {"parent_id": "tab", "protocol": "ads", "points": {}},
            },
        )


def test_incomplete_new_point_rejected(tmp_path):
    with pytest.raises(ConfigError, match="business point|address"):
        _load(
            tmp_path,
            {
                "tab": {"protocol": "modbus", "points": {}},
                "child": {"parent_id": "tab", "points": {"p1": {"point_groups": ["g"]}}},
            },
        )
