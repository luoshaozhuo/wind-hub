"""风机控制输入执行后对物理输出的独立动态响应验收。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import pytest
from pymodbus.client import ModbusTcpClient

from tests.third_party.dc_ems.prepare_wtg_control_chain import CONTROL_PORT
from tests.third_party.dc_ems.test_control_chain_live import _cli, _events, _request_id
from tests.third_party.dc_ems.test_wtg_control_chain_live import (
    _assert_applied,
    _read_power,
    _terminal_request,
    _words_cdab,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module", autouse=True)
def require_physical_test() -> None:
    """真实连续物理响应测量必须显式开启，不可拿 APPLIED 替代。"""
    if os.environ.get("DCEMS_WTG_PHYSICS_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_WTG_PHYSICS_LIVE=1 required")


def _model_control(generation_id: str, point: str, value: str) -> None:
    """仅设置受测物理场景的初始本地模式，不代替后续 DCEMS 工业控制。"""
    outcome = _cli(
        "data-generator", "variables", "update",
        "--generation-id", generation_id,
        "--device-id", "WTG_SIM01",
        "--request-id", f"fixture-{uuid4().hex}",
        "--point-id", point,
        "--value", value,
        "--yes",
    )
    assert outcome["status"] == "SUCCESS", outcome


def _power_samples(seconds: float) -> list[float]:
    """从 Gateway2 实时读取同一物理风机的实际电功率。"""
    samples: list[float] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        samples.append(_read_power(15421, 38))
        time.sleep(0.25)
    assert samples
    return samples


def test_dcems_wtg_power_command_changes_physical_output() -> None:
    """固定输入条件后比较两段实际输出，排除只更新控制寄存器的假阳性。"""
    root = Path(os.environ.get("DCEMS_CONTROL_ROOT", str(ROOT / "run/dcems-control-wtg")))
    generation_id = (root / "state/generation-id.txt").read_text(encoding="utf-8").strip()
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]

    # 该物理模型在 mode=0 时不使用 active_power_setpoint。
    # 设置为独立风机恒功率模式是测试前置条件，不修改生产默认策略。
    _model_control(generation_id, "control_mode", "1")
    _model_control(generation_id, "active_power_setpoint", "150000")
    baseline = _power_samples(5.0)

    prior = {event.event_id for event in _events(token)}
    client = ModbusTcpClient("127.0.0.1", port=CONTROL_PORT, timeout=5)
    try:
        assert client.connect()
        sent = client.write_registers(41557, _words_cdab(650.0), device_id=1)
        assert not sent.isError(), sent
    finally:
        client.close()
    request_id = ""
    for _ in range(100):
        request_id = _request_id(prior, token) or ""
        if request_id:
            break
        time.sleep(0.1)
    assert request_id, "no Modbus control event"
    request = _terminal_request(request_id)
    assert request["state"] == "SUCCEEDED", request
    assert request["io_settled"] is True
    command_id = str(request["commands"][0]["command_id"])
    assert _assert_applied(command_id, generation_id) > 0
    response = _power_samples(10.0)

    # 固定风速时指令应提高电功率。若模型达到饱和或控制链延迟，
    # 测试应报告真实证据而不是修改模型去凑结果。
    initial = sum(baseline[-8:]) / len(baseline[-8:])
    final = sum(response[-8:]) / len(response[-8:])
    evidence = {
        "initial_setpoint_w": 150000,
        "dcems_protocol_setpoint_kw": 650.0,
        "normalized_setpoint_w": 650000,
        "baseline_power_w": baseline,
        "response_power_w": response,
        "baseline_tail_mean_w": initial,
        "response_tail_mean_w": final,
        "command_id": command_id,
    }
    output = root / "state" / "wtg-physical-response.json"
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert final > initial + 20000, evidence
