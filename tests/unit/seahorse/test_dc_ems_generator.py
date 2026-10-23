"""原始 49 项逻辑映射编译为 45 个非重叠物理地址。"""

from __future__ import annotations

import json
from pathlib import Path

from industrial_gateway.infrastructure.config.loader import load_config
from seahorse.infrastructure.dc_ems_generator import generate_profile


def test_generate_50_turbine_gateway_profile(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3] / "configs" / "seahorse_dc_ems_manifest.json"
    assert len(json.loads(source.read_text(encoding="utf-8"))["points"]) == 49
    generate_profile(source, tmp_path)
    gateway = load_config(tmp_path / "gateway.yaml")
    assert len(gateway.devices) == 50
    assert len(gateway.endpoints) == 50
    assert len(gateway.point_tables["dc_ems_turbine"].points) == 45
    assert gateway.control.enabled is False
    assert gateway.point_tables["dc_ems_turbine"].points["ctrlActPow"].role == "command"
    assert gateway.point_tables["dc_ems_turbine"].points["emsPowAct"].role == "feedback"
    assert "emsActPowerSet" not in gateway.point_tables["dc_ems_turbine"].points
