"""真实 DCEMS Server/Web 控制输出通过 Modbus FC16 到 DG 的验收。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, cast
from urllib.request import Request, urlopen

import pytest

from tests.third_party.dc_ems.test_control_chain_live import _cli, _events, _request_id
from tests.third_party.dc_ems.test_wtg_control_chain_live import (
    _assert_applied,
    _terminal_request,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module", autouse=True)
def require_server() -> None:
    """显式授权真实 DCEMS Server 工业控制测试，严禁误连接生产环境。"""
    if os.environ.get("DCEMS_WTG_DCEMS_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_WTG_DCEMS_LIVE=1 required")
    if os.environ.get("DCEMS_ALLOW_LOCAL_SMOKE") != "1":
        pytest.skip("NOT_RUN: isolated DCEMS Server opt-in required")


def _dcems_set_power(value_kw: float) -> dict[str, Any]:
    """调用真实 DCEMS 控制参数入口；其成功回执不代表 Modbus 执行成功。"""
    url = "http://127.0.0.1:18090/api/config/setParam"
    payload = json.dumps(
        {"param": {"WTG01_ctrlActPow": value_kw}}, allow_nan=False
    ).encode("utf-8")
    request = Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        assert response.status == 200
        assert "json" in response.headers.get("Content-Type", "").lower()
        return cast(dict[str, Any], json.loads(response.read(65536)))


def test_real_dcems_server_wtg_power_command_reaches_dg() -> None:
    """真实 JAR/Web→Modbus→Gateway1→Commander→Gateway2→DG，逐级验证。"""
    base = Path(os.environ.get("DCEMS_CONTROL_ROOT", str(ROOT / "run/dcems-control-wtg")))
    generation_id = (base / "state/generation-id.txt").read_text(encoding="utf-8").strip()
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    prior = {event.event_id for event in _events(token)}
    web_result = _dcems_set_power(450.0)
    assert isinstance(web_result, dict)

    event_id = ""
    for _ in range(150):
        event_id = _request_id(prior, token) or ""
        if event_id:
            break
        time.sleep(0.1)
    assert event_id, (
        "DCEMS Web response did not result in an actual Modbus write; "
        "check DCEMS comOk, seed, Docker→Gateway reachability and Server logs"
    )
    request = _terminal_request(event_id)
    assert request["request_id"] == event_id
    assert request["state"] == "SUCCEEDED", request
    assert request["io_settled"] is True, request
    assert len(request["commands"]) == 1
    command_id = str(request["commands"][0]["command_id"])
    command = _cli("commander", "result", command_id)["data"]
    assert command["device_id"] == "WTG_SIM01"
    assert command["point_id"] == "active_power_setpoint"
    assert command["outcome"] == "CONFIRMED_SUCCESS"
    assert _assert_applied(command_id, generation_id) > 0

    evidence = {
        "source": "dc-ems Server /api/config/setParam",
        "dcems_device_id": "WTG01",
        "windhub_device_id": "WTG_SIM01",
        "dcems_point_id": "ctrlActPow",
        "commanded_kw": 450.0,
        "expected_dg_w": 450000.0,
        "gateway_event_id": event_id,
        "commander_command_id": command_id,
        "commander_outcome": command["outcome"],
        "generator_state": "APPLIED",
        "dcems_web_ack_is_not_execution_proof": True,
    }
    (base / "state" / "dcems-server-control-evidence.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )
