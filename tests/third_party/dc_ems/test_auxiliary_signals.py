"""验证十风机并网点与一次调频模式的实际可配置协议映射。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.third_party.dc_ems.audit_external_signals import SIGNALS, audit
from tests.third_party.dc_ems.auxiliary_seed import append_auxiliary_devices
from tests.third_party.dc_ems.prepare_seed import build_seed, load_mapping
from tests.third_party.dc_ems.stage_config import SOURCE
from tests.third_party.dc_ems.validate_gateway_mapping import validate


ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "third_party" / "dc-ems" / "app" / "server" / "data.json"


def test_station_and_pfr_mode_are_bound_via_real_gateway_addresses() -> None:
    original = json.loads(SEED.read_text(encoding="utf-8"))
    mapping = load_mapping(SOURCE / "mapping_random_smoke.csv")
    ten_wtg = build_seed(original, mapping, "127.0.0.1", 15221, allow_subset=True)
    expanded = append_auxiliary_devices(ten_wtg)
    assert len(expanded["devices"]) == 12
    assert len(expanded["edgeServers"]) == 10
    assert len(expanded["edgeaddrset"]) == 4
    assert {
        str(item["deviceType"]) for item in expanded["devices"]
    } == {"0", "1", "2"}
    assert [(p["varPath"], p["addr"], p["coe"]) for p in expanded["edgeaddrset"][1:]] == [
        ("powAct", "1.3.2", 0.001),
        ("powReact", "1.3.4", 0.001),
        ("isFreMode", "1.1.0", None),
    ]
    assert all(
        item["serverId"] == 1
        for item in expanded["devices"]
        if str(item["deviceType"]) in {"1", "2"}
    )
    contract = yaml.safe_load(SIGNALS.read_text(encoding="utf-8"))
    report = audit(expanded, contract)
    status = {record["id"]: record["status"] for record in report["records"]}
    assert status["plant_active_power"] == "BOUND"
    assert status["plant_reactive_power"] == "BOUND"
    assert status["pfr_mode"] == "BOUND"
    assert status["agc_grid_target"] == "UNPROVEN_FACTORY_INPUT"
    assert status["pfr_target_power"] == "UNPROVEN_FACTORY_INPUT"
    assert report["all_external_inputs_bound"] is False
    table = yaml.safe_load(
        (SOURCE / "point_table_dcems_wtg_random_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )
    validate(expanded, mapping, table)
    flags = {
        item["varPath"]: item["val"]
        for item in expanded["emsConfig"]
        if item["varPath"] in {
            "emsActPowerEnable",
            "emsReactPowerEnable",
            "bFreCtrlEnableFlag",
        }
    }
    assert flags == {
        "emsActPowerEnable": "false",
        "emsReactPowerEnable": "false",
        "bFreCtrlEnableFlag": "false",
    }


def test_gateway_types_and_units_are_correct() -> None:
    points = yaml.safe_load(
        (SOURCE / "point_table_dcems_wtg_random_v1.0.0.yaml").read_text(
            encoding="utf-8"
        )
    )["points"]
    assert points["sim_plant_active_power"]["address"]["address"] == 2
    assert points["sim_plant_active_power"]["unit"] == "watt"
    assert points["sim_plant_reactive_power"]["address"]["address"] == 4
    assert points["sim_plant_reactive_power"]["unit"] == "var"
    assert points["primary_frequency_mode"]["data_type"] == "bool"
    assert points["primary_frequency_mode"]["address"]["register_type"] == "discrete_input"
