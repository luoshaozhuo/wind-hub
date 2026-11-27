"""DCEMS Server 专用单风机种子：严格保持发布的 Modbus 地址与访问方向。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from tests.third_party.dc_ems.prepare_wtg_control_chain import prepare
from tests.third_party.dc_ems.prepare_wtg_dcems_seed import (
    CONTROL_FLAGS,
    DCEMS_DEVICE_ID,
    PROTOCOL_POINTS,
    SOURCE,
    build_seed,
)


def _staged_table(config: Path) -> dict[str, Any]:
    """读取生成根中的 WTG 目标点表。"""
    path = config / "templates/point_tables/" "point_table_modbus_wtg_dcems_control_v1.2.0.yaml"
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def test_real_dcems_seed_has_minimal_control_surface(tmp_path: Path) -> None:
    """只保留单台 WTG 的实际读写点，其他场站算法关闭。"""
    original = json.loads(SOURCE.read_text(encoding="utf-8"))
    seed = build_seed(original, "127.0.0.1")
    assert len(seed["devices"]) == len(seed["edgeServers"]) == 1
    device = seed["devices"][0]
    assert device["deviceId"] == DCEMS_DEVICE_ID
    assert "_" not in device["deviceId"], "DCEMS Web route splits deviceId_varPath"
    assert seed["edgeServers"][0]["port1"] == 15531
    assert len(seed["edgeaddrset"]) == 2
    assert {item["varPath"] for item in seed["edgeaddrset"]} == PROTOCOL_POINTS
    assert seed["syncdata"] == []
    assert {
        item["varPath"]: item["val"]
        for item in seed["emsConfig"]
        if item["varPath"] in CONTROL_FLAGS
    } == {name: "false" for name in CONTROL_FLAGS}

    config = prepare(tmp_path / "dcems-control-wtg")
    point_table = _staged_table(config)["points"]
    expected = {
        "ctrlActPow": ("active_power_setpoint", "1.4.41557", True),
        "ctrlReActPow": ("reactive_power_setpoint", "1.4.41555", True),
    }
    for point in seed["edgeaddrset"]:
        target, address, writable = expected[point["varPath"]]
        assert point["addr"] == address
        if writable:
            assert point["isCtrl"] is True
        assert point_table[target]["address"]["address"] == int(address.split(".")[-1])
        assert point_table[target]["address"]["register_type"] == "holding"
        assert point_table[target]["address"]["word_order"] == "little_endian"


@pytest.mark.parametrize("host", ["example.com", "192.168.1.3", "0.0.0.0", ""])
def test_dcems_seed_disallows_nonisolated_targets(host: str) -> None:
    """不允许测试种子将控制命令发往未知主机。"""
    with pytest.raises(ValueError):
        build_seed(json.loads(SOURCE.read_text(encoding="utf-8")), host)
