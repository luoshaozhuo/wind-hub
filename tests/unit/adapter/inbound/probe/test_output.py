"""Unit tests for ``cli/probe/output.py`` — YAML 草稿渲染。"""

from __future__ import annotations

import yaml

from wind_hub.adapter.inbound.cli.probe.models import DiscoveredPoint
from wind_hub.adapter.inbound.cli.probe.output import render_yaml_draft


def _ads_points() -> list[DiscoveredPoint]:
    return [
        DiscoveredPoint(symbol="MAIN.风机1.转速", data_type="float32", size=4, comment="转速"),
        DiscoveredPoint(symbol="MAIN.bRunning", data_type="bool", size=1),
    ]


def _parse(draft: str, table: str) -> list[dict]:
    """解析草稿并返回默认表（表名取设备 id）下的点位条目。"""
    data = yaml.safe_load(draft)
    assert list(data) == ["point_tables"]
    return data["point_tables"][table]["points"]


def test_render_yaml_draft_structure_compatible_with_points_yaml() -> None:
    """草稿顶层为 point_tables——点表设备无关，条目不含 device_id。"""
    draft = render_yaml_draft(device_id="plc-001", points=_ads_points(), protocol="ads")
    entries = _parse(draft, "plc-001")
    assert len(entries) == 2
    assert entries[0] == {
        "point_id": "风机1.转速",
        "address": {"symbol": "MAIN.风机1.转速"},
        "data_type": "float32",
        "unit": None,
        "description": "转速",
    }


def test_render_yaml_draft_header_comment() -> None:
    draft = render_yaml_draft(device_id="plc-001", points=_ads_points(), protocol="ads")
    assert draft.startswith("# 此文件由 wind-hub probe discover 生成，需人工确认")
    assert "unknown" in draft.splitlines()[1]


def test_render_yaml_draft_symbol_addressing() -> None:
    entries = _parse(render_yaml_draft("plc-001", _ads_points(), "ads"), "plc-001")
    assert entries[1]["address"] == {"symbol": "MAIN.bRunning"}
    assert entries[1]["point_id"] == "brunning"


def test_render_yaml_draft_register_addressing_with_scan_warning() -> None:
    points = [
        DiscoveredPoint(
            symbol="holding[0]",
            data_type="int16",
            size=2,
            comment="扫描结果，需人工确认",
            address={"register_type": "holding", "address": 0},
        )
    ]
    draft = render_yaml_draft(device_id="wtg-002", points=points, protocol="modbus")
    assert "寄存器扫描结果" in draft
    entries = _parse(draft, "wtg-002")
    assert entries[0] == {
        "point_id": "holding_0",
        "address": {"register_type": "holding", "address": 0},
        "data_type": "int16",
        "unit": None,
        "description": "扫描结果，需人工确认",
    }


def test_render_yaml_draft_empty_points() -> None:
    assert _parse(render_yaml_draft("plc-001", [], "ads"), "plc-001") == []
