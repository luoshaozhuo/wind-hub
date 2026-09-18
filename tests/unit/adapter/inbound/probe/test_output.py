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


def _parse(draft: str) -> dict:
    return yaml.safe_load(draft)


def test_render_yaml_draft_structure_compatible_with_points_yaml() -> None:
    draft = render_yaml_draft(device_id="plc-001", points=_ads_points(), protocol="ads")
    data = _parse(draft)
    assert list(data) == ["points"]
    assert len(data["points"]) == 2
    first = data["points"][0]
    assert first == {
        "point_id": "风机1.转速",
        "device_id": "plc-001",
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
    data = _parse(render_yaml_draft("plc-001", _ads_points(), "ads"))
    assert data["points"][1]["address"] == {"symbol": "MAIN.bRunning"}
    assert data["points"][1]["point_id"] == "brunning"


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
    data = _parse(draft)
    assert data["points"][0] == {
        "point_id": "holding_0",
        "device_id": "wtg-002",
        "address": {"register_type": "holding", "address": 0},
        "data_type": "int16",
        "unit": None,
        "description": "扫描结果，需人工确认",
    }


def test_render_yaml_draft_empty_points() -> None:
    data = _parse(render_yaml_draft("plc-001", [], "ads"))
    assert data["points"] == []
