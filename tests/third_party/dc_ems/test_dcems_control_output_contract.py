"""审查 DCEMS 原始工业协议控制出口，防止误接 PPC 和伪造 AGC 输入。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from data_generator.application.model.inputs import writable_inputs

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = (
    ROOT / "tests/third_party/dc-ems/config/dcems_control_output_contract_v1.0.0.yaml"
)
SEED = ROOT / "third_party/dc-ems/app/server/data.json"
DATA_TYPES = {"F": ("float32", 2), "I": ("int32", 2), "B": ("bool", 1)}
AREAS = {"0": "coil", "4": "holding"}


def _contract() -> dict[str, Any]:
    """读取隔离的控制协议合同。"""
    return cast(dict[str, Any], yaml.safe_load(CONTRACT.read_text(encoding="utf-8")))


def _control_points() -> dict[str, dict[str, Any]]:
    """从 DCEMS 发布种子取得真正可写的地址，而不是 emsPoint 变量字典。"""
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    controls = [point for point in seed["edgeaddrset"] if point.get("isCtrl") is True]
    return {str(point["varPath"]): point for point in controls}


def test_dcems_control_outputs_match_published_modbus_seed() -> None:
    """每个可写点的协议区、地址、类型、字序必须精确一致。"""
    contract = _contract()
    controls = _control_points()
    outputs = contract["outputs"]
    assert set(controls) == set(outputs)
    assert contract["source"]["protocol"] == "modbus_tcp"
    assert contract["source"]["word_order"] == "little_endian"
    assert contract["source"]["byte_order"] == "big_endian"

    for name, expected in outputs.items():
        actual = controls[name]
        slave, register, offset = actual["addr"].split(".")
        assert int(slave) == contract["source"]["slave_id"], name
        assert int(offset) == expected["address"], name
        assert AREAS[register] == expected["register_type"], name
        assert DATA_TYPES[actual["dataType"]] == (
            expected["data_type"],
            expected["register_count"],
        ), name
        if register == "4":
            assert actual["highBit"] == 1, name
        assert actual["isCtrl"] is True, name
        assert expected["status"] in {"PROPOSED_UNVERIFIED", "NOT_MAPPED"}


@pytest.mark.parametrize(
    ("name", "target", "source_unit", "target_unit"),
    [
        ("ctrlActPow", "active_power_setpoint", "kilowatt", "watt"),
        ("ctrlReActPow", "reactive_power_setpoint", "kilovar", "var"),
    ],
)
def test_dcems_wtg_conversion_is_explicit_but_not_yet_proven(
    name: str, target: str, source_unit: str, target_unit: str
) -> None:
    """确认候选目标真实存在；此检查不能充当实际物理执行验收。"""
    output = _contract()["outputs"][name]
    assert output["status"] == "PROPOSED_UNVERIFIED"
    assert output["dcems_unit"] == source_unit
    assert output["proposed_generator_point"] == target
    assert output["proposed_generator_unit"] == target_unit
    assert output["conversion_factor"] == 1000
    spec = writable_inputs("wtg")[target]
    assert spec.unit == target_unit
    assert spec.data_type == "float32"


def test_dcems_factory_inputs_are_not_modbus_control_outputs() -> None:
    """不允许将厂站 AGC/AVC/PFR 输入误判为现有 Modbus 设备出口。"""
    contract = _contract()
    outputs = set(contract["outputs"])
    missing = set(contract["unproven_factory_inputs"])
    assert missing == {
        "emsGridDispatchAct",
        "emsGridDispatchReact",
        "emsFreDst",
        "emsDeltapValue",
    }
    assert missing.isdisjoint(outputs)
    assert missing.isdisjoint(_control_points())
