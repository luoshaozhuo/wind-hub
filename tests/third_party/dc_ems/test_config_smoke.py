"""十设备随机场景、Gateway BusinessPoint 地址及 DCEMS 数据种子静态联验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.third_party.dc_ems.prepare_seed import build_seed, load_mapping
from tests.third_party.dc_ems.stage_config import SOURCE, stage
from tests.third_party.dc_ems.validate_gateway_mapping import validate


ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "third_party" / "dc-ems" / "app" / "server" / "data.json"


def _read(filename: str) -> dict:
    return yaml.safe_load((SOURCE / filename).read_text(encoding="utf-8"))


def test_ten_devices_and_gateways_have_unique_endpoints() -> None:
    devices = _read("device_dcems-smoke_v1.0.0.yaml")["devices"]
    gateway = _read("service_gateway-dcems-smoke_v1.0.0.yaml")
    servers = gateway["servers"]
    assert len(devices) == len(servers) == 10
    expected_ids = {f"DCEMS_WTG{index:02d}" for index in range(1, 11)}
    assert set(devices) == {server["device_id"] for server in servers} == expected_ids
    ports = {server["listen_port"] for server in servers}
    assert ports == set(range(15221, 15231))
    for index, server in enumerate(servers, 1):
        assert server["control"]["enabled"] is False
        assert devices[f"DCEMS_WTG{index:02d}"]["endpoint"]["port"] == 15220 + index


def test_random_scenario_targets_exact_gateway_business_names() -> None:
    scenario = _read("scenarios_dcems-smoke_v1.0.0.yaml")["scenarios"][0]
    bindings = scenario["gateways"]
    assert scenario["source"]["kind"] == "simulation"
    assert len(bindings) == len(scenario["devices"]) == 10
    assert {row["device_id"] for row in bindings} == {
        row["device_id"] for row in scenario["devices"]
    }
    assert all(row["variable_mapping"] == {"wind_speed": "wind_speed"} for row in bindings)
    point_table = _read("point_table_dcems_wtg_random_v1.0.0.yaml")
    point = point_table["points"]["wind_speed"]
    assert point["business_point_id"] == "wind_speed"
    assert point["address"]["register_type"] == "input"
    assert point["address"]["address"] == 0
    assert point["address"]["word_order"] == "big_endian"


def test_derived_dcems_ten_device_seed_matches_gateway_modbus_contract() -> None:
    original = json.loads(SEED.read_text(encoding="utf-8"))
    mapping = load_mapping(SOURCE / "mapping_random_smoke.csv")
    derived = build_seed(
        original, mapping, "127.0.0.1", 15221, device_count=10, allow_subset=True
    )
    assert len(derived["devices"]) == len(derived["edgeServers"]) == 10
    assert len(derived["edgeaddrset"]) == 1
    assert derived["edgeaddrset"][0]["varPath"] == "emsWinSpd"
    assert derived["edgeaddrset"][0]["addr"] == "1.3.0"
    assert derived["edgeaddrset"][0]["highBit"] == 0
    validate(derived, mapping, _read("point_table_dcems_wtg_random_v1.0.0.yaml"))


def test_stage_root_has_single_selected_deployment(tmp_path: Path) -> None:
    root = tmp_path / "dcems-smoke"
    stage(root)
    assert (
        root / "deployments" / "deployment_dcems-smoke_v1.0.0.yaml"
    ).is_file()
    assert (
        root / "templates" / "point_tables" / "point_table_dcems_wtg_random_v1.0.0.yaml"
    ).is_file()
    with pytest.raises(FileExistsError):
        stage(root)
