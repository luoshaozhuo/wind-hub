"""原始 49 项逻辑映射编译为 45 个非重叠物理地址。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_external_device_requires_source_backed_mapping(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3] / "configs" / "seahorse_dc_ems_manifest.json"
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["external_devices"] = [
        {
            "id": "substation",
            "model_id": "substation",
            "server_id": 51,
            "ip": "192.168.1.100",
            "original_port": 502,
            "port": 15170,
            "unit_id": 1,
            "points": [
                {
                    "name": "emsSyzAct",
                    "role": "feedback",
                    "data_type": "float32",
                    "register_type": "holding",
                    "address": 100,
                    "word_order": "little_endian",
                }
            ],
        }
    ]
    source_file = tmp_path / "source.json"
    source_file.write_text(json.dumps(manifest), encoding="utf-8")
    generate_profile(source_file, tmp_path / "generated")
    gateway = load_config(tmp_path / "generated" / "gateway.yaml")
    assert len(gateway.devices) == 51
    assert "substation" in gateway.devices
    assert gateway.point_tables["external_substation"].points["emsSyzAct"].role == "feedback"


def test_external_mapping_rejects_invalid_addresses(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[3] / "configs" / "seahorse_dc_ems_manifest.json"
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["external_devices"] = [
        {
            "id": "substation",
            "model_id": "substation",
            "server_id": 51,
            "ip": "192.168.1.100",
            "original_port": 502,
            "port": 15120,
            "unit_id": 1,
            "points": [
                {
                    "name": "emsSyzAct",
                    "role": "feedback",
                    "data_type": "float32",
                    "register_type": "holding",
                    "address": 100,
                    "word_order": "little_endian",
                }
            ],
        }
    ]
    source_file = tmp_path / "source.json"
    source_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate listen endpoint"):
        generate_profile(source_file, tmp_path / "generated")
    assert not (tmp_path / "generated" / "gateway.yaml").exists()
