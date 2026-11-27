"""风机控制输入执行后的真实物理响应、稳态与能量一致性验收。"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import pytest
import yaml
from pymodbus.client import ModbusTcpClient

from contracts import industrial_gateway_pb2 as gateway_pb  # type: ignore[import-untyped]
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from contracts.scalar_codec import decode_scalar  # type: ignore[import-untyped]
from tests.third_party.dc_ems.prepare_wtg_control_chain import CONTROL_PORT
from tests.third_party.dc_ems.test_control_chain_live import _cli, _events, _request_id
from tests.third_party.dc_ems.test_wtg_control_chain_live import (
    _assert_applied,
    _terminal_request,
    _words_cdab,
)

ROOT = Path(__file__).resolve().parents[3]
EXECUTION_GRPC = 52173
GATEWAY_ID = "gw-wtg-sim01"
DEVICE_ID = "WTG_SIM01"
STATE_VARIABLES = (
    "active_power",
    "sim_rotor_speed",
    "tip_speed_ratio",
    "power_coefficient",
    "pitch_angle_1",
    "sim_generator_torque",
    "aerodynamic_power",
    "available_power",
    "wind_speed",
    "control_mode",
    "active_power_setpoint",
    "fault_status",
    "running_status",
)
SAMPLE_SECONDS = 0.5
# 4 s 稳态窗覆盖转矩一阶时间常数 0.5 s，并要求连续 8 个采样满足误差带。
STEADY_WINDOW = 8
# 全行程变桨 1.57 rad / 0.12 rad/s ≈ 13.1 s，留出转速与转矩稳定余量。
MAX_SETTLE_SECONDS = 30.0
EPSILON = 1e-9


@pytest.fixture(scope="module", autouse=True)
def require_physical_test() -> None:
    """真实连续物理响应测量必须显式开启，不可拿 APPLIED 替代。"""
    if os.environ.get("DCEMS_WTG_PHYSICS_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_WTG_PHYSICS_LIVE=1 required")


def _root() -> Path:
    return Path(os.environ.get("DCEMS_CONTROL_ROOT", str(ROOT / "run" / "dcems-control-wtg")))


def _generation_id() -> str:
    return (_root() / "state" / "generation-id.txt").read_text(encoding="utf-8").strip()


def _model_params() -> dict[str, Any]:
    path = (
        _root()
        / "configs"
        / "templates"
        / "device_models"
        / "device_model_wtg_dcems_control_v1.2.0.yaml"
    )
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(document["simulation"]["parameters"])


def _weather_config() -> dict[str, Any]:
    path = _root() / "configs" / "instances" / "scenarios_e2e-final_v1.0.0.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    scenario = next(
        item for item in document["scenarios"] if item["scenario_id"] == "e2e-plant-physical"
    )
    return dict(scenario["weather"]["site-a"])


def _state() -> dict[str, Any]:
    """由 Gateway2 受授权接口读取模型全部物理状态。"""
    token = os.environ["WIND_HUB_E2E_GATEWAY_OPERATOR_TOKEN"]
    channel = grpc.insecure_channel(f"127.0.0.1:{EXECUTION_GRPC}")
    try:
        result = gateway_rpc.IndustrialGatewayServiceStub(channel).ReadVariables(
            gateway_pb.ReadVariablesRequest(
                gateway_id=GATEWAY_ID, variable_ids=list(STATE_VARIABLES)
            ),
            metadata=(("authorization", f"Bearer {token}"),),
            timeout=5,
        )
        return {sample.point_id: decode_scalar(sample.value) for sample in result.values}
    finally:
        channel.close()


def _model_inputs(values: dict[str, Any]) -> None:
    """仅通过正式 CLI 修改隔离 Generation 的模型输入。"""
    generation_id = _generation_id()
    for point, value in values.items():
        outcome = _cli(
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            generation_id,
            "--device-id",
            DEVICE_ID,
            "--request-id",
            f"fixture-{uuid4().hex}",
            "--point-id",
            point,
            "--value",
            str(value),
            "--yes",
        )
        assert outcome["status"] == "SUCCESS", outcome


def _issue_setpoint_power(kw: float) -> dict[str, Any]:
    """真实 FC16 下发有功设定值并取得 Commander/DG 执行证据。"""
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    prior = {event.event_id for event in _events(token)}
    client = ModbusTcpClient("127.0.0.1", port=CONTROL_PORT, timeout=5)
    try:
        assert client.connect()
        sent = client.write_registers(41557, _words_cdab(kw), device_id=1)
        assert not sent.isError(), sent
        readback = client.read_holding_registers(41557, count=2, device_id=1)
        assert not readback.isError()
        assert readback.registers == _words_cdab(kw)
    finally:
        client.close()
    request_id = ""
    for _ in range(150):
        request_id = _request_id(prior, token) or ""
        if request_id:
            break
        time.sleep(0.1)
    assert request_id, "no Gateway1 protocol write event"
    request = _terminal_request(request_id)
    assert request["state"] == "SUCCEEDED", request
    assert request["io_settled"] is True, request
    assert len(request["commands"]) == 1, request
    command_id = str(request["commands"][0]["command_id"])
    command = _cli("commander", "result", command_id)["data"]
    assert command["outcome"] == "CONFIRMED_SUCCESS", command
    assert command["device_id"] == DEVICE_ID
    assert command["point_id"] == "active_power_setpoint"
    effective_step = _assert_applied(command_id, _generation_id())
    return {
        "requested_kw": kw,
        "request_id": request_id,
        "command_id": command_id,
        "commander_state": command["state"],
        "commander_outcome": command["outcome"],
        "dg_state": "APPLIED",
        "dg_effective_step": effective_step,
        "source_registers": list(readback.registers),
    }


def _settle(target_w: float, label: str) -> dict[str, Any]:
    """按功率稳态条件等待，不使用固定秒数；返回时间序列与稳态统计。"""
    tolerance = 1000.0 if target_w == 0 else max(abs(target_w) * 0.01, 2000.0)
    samples: list[dict[str, Any]] = []
    started = time.monotonic()
    settled_at: float | None = None
    while time.monotonic() - started < MAX_SETTLE_SECONDS:
        elapsed = time.monotonic() - started
        samples.append({"elapsed_s": round(elapsed, 3), **_state()})
        recent = samples[-STEADY_WINDOW:]
        if len(recent) == STEADY_WINDOW and all(
            abs(float(item["active_power"]) - target_w) <= tolerance for item in recent
        ):
            settled_at = float(samples[-1]["elapsed_s"])
            break
        time.sleep(SAMPLE_SECONDS)
    assert settled_at is not None, {
        "label": label,
        "target_w": target_w,
        "tolerance_w": tolerance,
        "last_powers": [item["active_power"] for item in samples[-STEADY_WINDOW:]],
        "max_settle_seconds": MAX_SETTLE_SECONDS,
    }
    tail = samples[-STEADY_WINDOW:]
    power_mean = sum(float(item["active_power"]) for item in tail) / len(tail)
    return {
        "label": label,
        "target_w": target_w,
        "tolerance_w": tolerance,
        "settled_s": settled_at,
        "sample_count": len(samples),
        "steady_mean_w": power_mean,
        "steady_error_w": power_mean - target_w,
        "steady_min_w": min(float(item["active_power"]) for item in tail),
        "steady_max_w": max(float(item["active_power"]) for item in tail),
        "series": samples,
    }


def _assert_physical_consistency(
    step: dict[str, Any],
    *,
    rated_w: float,
    gear: float,
    eta: float,
    radius: float,
    rho: float,
) -> None:
    """逐点校验电功率、气动功率、叶尖速比与安全保护。"""
    for item in step["series"]:
        power = float(item["active_power"])
        rotor = float(item["sim_rotor_speed"])
        wind = float(item["wind_speed"])
        cp = float(item["power_coefficient"])
        torque = float(item["sim_generator_torque"])
        aero = float(item["aerodynamic_power"])
        pitch = float(item["pitch_angle_1"])
        assert item["fault_status"] in (0, 0.0), item
        assert item["running_status"] in (1, 1.0), item
        assert -EPSILON <= power <= rated_w * 1.000001, item
        assert 0.0 <= rotor <= 2.0 + EPSILON, item
        assert 0.0 <= pitch <= math.degrees(1.57) + 1e-6, item
        assert torque >= 0.0, item
        assert cp >= 0.0, item
        expected_power = eta * torque * gear * rotor
        assert abs(power - expected_power) <= max(abs(power) * 0.01, 200.0), (
            power,
            expected_power,
        )
        if wind > 0:
            expected_lambda = rotor * radius / wind
            assert abs(float(item["tip_speed_ratio"]) - expected_lambda) <= max(
                abs(expected_lambda) * 0.01, 1e-6
            ), item
            expected_aero = 0.5 * rho * math.pi * radius**2 * cp * wind**3
            assert abs(aero - expected_aero) <= max(abs(expected_aero) * 0.02, 1000.0), (
                aero,
                expected_aero,
            )


def test_dcems_wtg_power_command_tracks_physical_output() -> None:
    """固定工况下验证升/降/零/额定边界及 mode 0/1/2 的物理语义。"""
    params = _model_params()
    weather = _weather_config()
    rho = float(weather["wind"]["air_density"])
    radius = float(params["rotor_radius_m"])
    gear = float(params["gear_ratio"])
    eta = float(params["generator_efficiency"]) * float(params["converter_efficiency"])
    rated_w = float(params["rated_power_w"])
    initial = _state()
    assert (
        float(initial["control_mode"]) == 1
    ), "run_wtg_control_chain.sh --up must initialize WTG_SIM01.control_mode=1"
    assert float(initial["wind_speed"]) == float(weather["wind"]["wind_speed_mean"]), initial

    evidence: dict[str, Any] = {
        "weather": weather,
        "model_parameters": {
            "rated_power_w": rated_w,
            "rotor_radius_m": radius,
            "gear_ratio": gear,
            "eta": eta,
        },
        "steady_criterion": {
            "window_samples": STEADY_WINDOW,
            "sample_seconds": SAMPLE_SECONDS,
            "max_settle_seconds": MAX_SETTLE_SECONDS,
            "tolerance": "1% target or 2 kW; zero target 1 kW",
        },
        "steps": [],
    }
    output = _root() / "state" / "wtg-physical-response.json"
    try:
        for label, target_kw in (("baseline_150k", 150.0), ("step_up_650k", 650.0)):
            command = _issue_setpoint_power(target_kw)
            step = _settle(target_kw * 1000.0, label)
            step["command"] = command
            _assert_physical_consistency(
                step, rated_w=rated_w, gear=gear, eta=eta, radius=radius, rho=rho
            )
            evidence["steps"].append(step)

        baseline = evidence["steps"][0]
        step_up = evidence["steps"][1]
        assert step_up["steady_mean_w"] > baseline["steady_mean_w"] + 400_000.0, evidence

        for label, target_kw in (
            ("step_down_150k", 150.0),
            ("zero_power", 0.0),
            ("rated_2000k", 2000.0),
        ):
            command = _issue_setpoint_power(target_kw)
            step = _settle(target_kw * 1000.0, label)
            step["command"] = command
            _assert_physical_consistency(
                step, rated_w=rated_w, gear=gear, eta=eta, radius=radius, rho=rho
            )
            evidence["steps"].append(step)

        # mode 0 语义：最优功率跟踪，忽略有功设定值。
        _model_inputs({"control_mode": 0, "active_power_setpoint": 150000})
        mode_zero = _settle(rated_w, "mode0_max_power")
        mode_zero["setpoint_w"] = 150000
        _assert_physical_consistency(
            mode_zero, rated_w=rated_w, gear=gear, eta=eta, radius=radius, rho=rho
        )
        assert mode_zero["steady_mean_w"] > rated_w * 0.95, mode_zero
        evidence["steps"].append(mode_zero)

        # mode 2 语义：按 power_limit_setpoint 限功率。
        _model_inputs({"control_mode": 2, "power_limit_setpoint": 650000})
        mode_two = _settle(650000.0, "mode2_650k_limit")
        mode_two["limit_w"] = 650000
        _assert_physical_consistency(
            mode_two, rated_w=rated_w, gear=gear, eta=eta, radius=radius, rho=rho
        )
        evidence["steps"].append(mode_two)
    finally:
        output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")
