"""防止将 DCEMS 的 FactoryPoints 算法输入误报为已由协议接入。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.third_party.dc_ems.audit_external_signals import SIGNALS, audit


ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "third_party/dc-ems/app/server/data.json"


def test_dcems_historical_seed_missing_agc_frequency_inputs() -> None:
    """事实性审查：缺失输入须显式报告 BLOCKED，不是采样点自动填充。"""
    document = json.loads(SEED.read_text(encoding="utf-8"))
    contract = yaml.safe_load(SIGNALS.read_text(encoding="utf-8"))
    result = audit(document, contract)
    status = {item["id"]: item["status"] for item in result["records"]}
    assert status["wt_wind_speed"] == "BOUND"
    assert status["plant_active_power"] == "MISSING_DEVICE_OR_POINT"
    assert status["plant_reactive_power"] == "MISSING_DEVICE_OR_POINT"
    assert status["pfr_mode"] == "MISSING_DEVICE_OR_POINT"
    assert status["agc_grid_target"] == "UNPROVEN_FACTORY_INPUT"
    assert status["pfr_target_power"] == "UNPROVEN_FACTORY_INPUT"
    assert status["grid_frequency"] == "UNPROVEN_FACTORY_INPUT"
    assert result["all_external_inputs_bound"] is False


def test_algorithm_input_is_not_proven_by_fake_same_named_edge_point() -> None:
    """在现有 wind turbine 地址集中添加同名点也不能证明 FactoryPoint 更新。"""
    document = json.loads(SEED.read_text(encoding="utf-8"))
    contract = yaml.safe_load(SIGNALS.read_text(encoding="utf-8"))
    point = dict(document["edgeaddrset"][0])
    point["varPath"] = "emsGridDispatchAct"
    document["edgeaddrset"].append(point)
    result = audit(document, contract)
    status = {item["id"]: item["status"] for item in result["records"]}
    assert status["agc_grid_target"] == "UNPROVEN_FACTORY_INPUT"
