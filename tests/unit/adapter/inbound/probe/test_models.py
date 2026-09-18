"""Unit tests for ``cli/probe/models.py`` — DiscoveredPoint 与符号名转换。

纯逻辑测试：不触网、不依赖 pyads/pymodbus。
"""

from __future__ import annotations

from wind_hub.adapter.inbound.cli.probe.models import DiscoveredPoint, symbol_to_point_id

# ---------------------------------------------------------------------------
# DiscoveredPoint
# ---------------------------------------------------------------------------


def test_discovered_point_creation_defaults() -> None:
    point = DiscoveredPoint(symbol="MAIN.风机1.转速", data_type="float32", size=4)
    assert point.symbol == "MAIN.风机1.转速"
    assert point.data_type == "float32"
    assert point.size == 4
    assert point.comment is None
    assert point.address is None


def test_to_yaml_dict_symbol_addressing() -> None:
    """无显式 address 时输出符号寻址（ADS 路径，决策 4）。"""
    point = DiscoveredPoint(
        symbol="MAIN.风机1.转速", data_type="float32", size=4, comment="转速符号注释"
    )
    entry = point.to_yaml_dict(point_id="风机1.转速")
    assert entry == {
        "point_id": "风机1.转速",
        "address": {"symbol": "MAIN.风机1.转速"},
        "data_type": "float32",
        "unit": None,
        "description": "转速符号注释",
    }


def test_to_yaml_dict_explicit_address() -> None:
    """显式 address（Modbus 扫描路径）原样输出，不用 symbol 寻址。"""
    point = DiscoveredPoint(
        symbol="holding[100]",
        data_type="int16",
        size=2,
        comment="扫描结果，需人工确认",
        address={"register_type": "holding", "address": 100},
    )
    entry = point.to_yaml_dict(point_id="holding_100")
    assert entry["address"] == {"register_type": "holding", "address": 100}


# ---------------------------------------------------------------------------
# symbol_to_point_id（决策 5）
# ---------------------------------------------------------------------------


def test_symbol_to_point_id_strips_main_prefix() -> None:
    assert symbol_to_point_id("MAIN.风机1.转速") == "风机1.转速"


def test_symbol_to_point_id_lowercases_and_keeps_dots() -> None:
    assert symbol_to_point_id("MAIN.GVL.nWindSpeed") == "gvl.nwindspeed"


def test_symbol_to_point_id_without_main_prefix() -> None:
    # 只有开头的 "MAIN." 才剥离
    assert symbol_to_point_id("GVL.Main.Value") == "gvl.main.value"


def test_symbol_to_point_id_replaces_special_chars() -> None:
    assert symbol_to_point_id("holding[100]") == "holding_100"
    assert symbol_to_point_id("MAIN.a b/c") == "a_b_c"


def test_symbol_to_point_id_keeps_chinese_and_underscore() -> None:
    assert symbol_to_point_id("MAIN.风机_1.转速") == "风机_1.转速"
