"""DCEMS 配置派生与 Gateway 地址别名静态回归。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest

from tests.third_party.dc_ems.prepare_seed import build_seed, load_mapping

SEED = (
    Path(__file__).resolve().parents[3] / "third_party" / "dc-ems" / "app" / "server" / "data.json"
)


@pytest.fixture
def original() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SEED.read_text(encoding="utf-8")))


@pytest.fixture
def bindings(original: dict[str, Any]) -> dict[str, dict[str, str]]:
    # 测试中使用合成业务 ID；正式部署不得将其作为真实 Gateway 映射。
    return {
        point["varPath"]: {
            "business_point_id": "test_" + point["addr"].replace(".", "_"),
            "modbus_addr": point["addr"],
        }
        for point in original["edgeaddrset"]
    }


def test_derived_seed_is_isolated(
    original: dict[str, Any], bindings: dict[str, dict[str, str]]
) -> None:
    snapshot = copy.deepcopy(original)
    derived = build_seed(original, bindings, "127.0.0.1", 15221)
    assert original == snapshot
    assert len(derived["devices"]) == 10
    assert len(derived["edgeServers"]) == 10
    assert {x["deviceId"] for x in derived["devices"]} == {
        f"WTG{index:02d}" for index in range(1, 11)
    }
    assert len({x["port1"] for x in derived["edgeServers"]}) == 10
    assert derived["edgeServers"][0]["serverIp1"] == "127.0.0.1"
    assert derived["edgeServers"][0]["port1"] == 15221
    assert derived["syncdata"] == []
    assert not any(
        row["groupName"] == "sync" and row["itemName"] == "redisCon" for row in derived["sysconfig"]
    )
    assert next(row for row in derived["emsConfig"] if row["varPath"] == "iWTUnits")["val"] == "10"


def test_missing_business_mapping_rejected(
    original: dict[str, Any], bindings: dict[str, dict[str, str]]
) -> None:
    bindings.pop("emsWinSpd")
    with pytest.raises(ValueError, match="not closed"):
        build_seed(original, bindings, "127.0.0.1", 15221)


def test_address_mismatch_rejected(
    original: dict[str, Any], bindings: dict[str, dict[str, str]]
) -> None:
    bindings["emsWinSpd"]["modbus_addr"] = "1.4.1"
    with pytest.raises(ValueError, match="address mismatch"):
        build_seed(original, bindings, "127.0.0.1", 15221)


def test_shared_register_alias_must_share_business_id(
    original: dict[str, Any], bindings: dict[str, dict[str, str]]
) -> None:
    assert (
        bindings["ctrlActPow"]["business_point_id"]
        == bindings["emsActPowerSet"]["business_point_id"]
    )
    bindings["ctrlActPow"]["business_point_id"] = "different_business"
    with pytest.raises(ValueError, match="aliases disagree"):
        build_seed(original, bindings, "127.0.0.1", 15221)


def test_business_id_cannot_represent_two_addresses(
    original: dict[str, Any], bindings: dict[str, dict[str, str]]
) -> None:
    bindings["emsWinSpd"]["business_point_id"] = bindings["emsPowAct"]["business_point_id"]
    with pytest.raises(ValueError, match="reused across addresses"):
        build_seed(original, bindings, "127.0.0.1", 15221)


def test_csv_input_rejects_duplicates(tmp_path: Path) -> None:
    mapping = tmp_path / "mapping.csv"
    mapping.write_text(
        "dcems_var_path,business_point_id,modbus_addr\n"
        "emsPowAct,active_power,1.4.41509\n"
        "emsPowAct,active_power,1.4.41509\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_mapping(mapping)


@pytest.mark.parametrize(
    "missing_flag", ["emsActPowerEnable", "emsReactPowerEnable", "bFreCtrlEnableFlag"]
)
def test_missing_control_enable_flag_rejected(
    original: dict[str, Any], bindings: dict[str, dict[str, str]], missing_flag: str
) -> None:
    """缺少自动控制禁用配置时不得生成测试种子。"""
    original["emsConfig"] = [row for row in original["emsConfig"] if row["varPath"] != missing_flag]
    with pytest.raises(ValueError, match="control enable flag"):
        build_seed(original, bindings, "127.0.0.1", 15221)


def test_duplicate_control_enable_flag_rejected(
    original: dict[str, Any], bindings: dict[str, dict[str, str]]
) -> None:
    """重复的控制配置不得通过种子派生。"""
    row = next(row for row in original["emsConfig"] if row["varPath"] == "emsActPowerEnable")
    original["emsConfig"].append(copy.deepcopy(row))
    with pytest.raises(ValueError, match="control enable flag"):
        build_seed(original, bindings, "127.0.0.1", 15221)
