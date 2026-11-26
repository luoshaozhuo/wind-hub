"""DCEMS 发布种子和 Gateway 业务点名称的一致性静态门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SEED = ROOT / "third_party" / "dc-ems" / "app" / "server" / "data.json"
REQUIRED = {
    "emsConfig",
    "emsPoint",
    "turbPoint",
    "pagepoints",
    "syncdata",
    "sysconfig",
    "edgeaddrset",
    "lineColors",
    "devices",
    "alarmConfigs",
    "edgeServers",
    "powCurves",
    "userInfos",
}


@pytest.fixture(scope="module")
def seed() -> dict[str, list[dict[str, object]]]:
    document = json.loads(SEED.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert document.keys() >= REQUIRED
    for key in REQUIRED:
        assert isinstance(document[key], list), key
    return document


def test_seed_device_and_channel_references(
    seed: dict[str, list[dict[str, object]]],
) -> None:
    devices = seed["devices"]
    servers = seed["edgeServers"]
    assert devices and servers
    identities = [device["deviceId"] for device in devices]
    assert len(identities) == len(set(identities))
    for device in devices:
        assert isinstance(device["serverId"], int)
        assert 1 <= device["serverId"] <= len(servers)
        assert any(row["setId"] == device["addrSet"] for row in seed["edgeaddrset"])


def test_seed_modbus_contract(seed: dict[str, list[dict[str, object]]]) -> None:
    points = seed["edgeaddrset"]
    assert points
    names = [point["varPath"] for point in points]
    assert len(names) == len(set(names))
    for point in points:
        address = str(point["addr"]).split(".")
        assert len(address) in {3, 4}
        assert all(part.isdecimal() for part in address)
        assert int(address[1]) in {0, 1, 3, 4}
        if point.get("isCtrl") is True:
            assert int(address[1]) in {0, 4}


def test_scada_redis_not_an_external_input(
    seed: dict[str, list[dict[str, object]]],
) -> None:
    """首版不允许以 SCADA Redis 作为外部输入通道。"""
    assert not any(row.get("ioType") == "I" for row in seed["syncdata"])


def test_release_seed_is_not_a_ready_to_run_dcems_scenario(
    seed: dict[str, list[dict[str, object]]],
) -> None:
    """种子原样使用会回到历史 PLC；本测试要求独立派生配置。"""
    servers = seed["edgeServers"]
    assert len(servers) == 50
    assert all(server["serverType"] == "MODBUSTCP" for server in servers)
    assert len(seed["edgeaddrset"]) == 49
    assert sum(row.get("isCtrl") is True for row in seed["edgeaddrset"]) == 9
