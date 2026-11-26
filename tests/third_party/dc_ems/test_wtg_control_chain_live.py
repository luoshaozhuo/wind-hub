"""真实 DCEMS 格式 FC16 风机控制 → Commander → DG 物理执行验收。"""

from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path
from typing import Any

import grpc
import pytest
from pymodbus.client import ModbusTcpClient

from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from tests.third_party.dc_ems.prepare_wtg_control_chain import CONTROL_PORT
from tests.third_party.dc_ems.test_control_chain_live import _cli, _events, _request_id

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module", autouse=True)
def live_only() -> None:
    """独立执行，不能静态配置代替真实协议。"""
    if os.environ.get("DCEMS_WTG_CONTROL_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_WTG_CONTROL_LIVE=1 required")


def _words_cdab(value: float) -> list[int]:
    """FC16：将工程数 F32 编为 DCEMS highBit=1 的 CDAB 两寄存器。"""
    first, second = struct.unpack(">HH", struct.pack(">f", value))
    return [second, first]


def _read_power(port: int, offset: int) -> float:
    """从真实 Gateway2 读取风机模型有功输出 W。"""
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=5)
    try:
        assert client.connect()
        response = client.read_input_registers(offset, count=2, device_id=1)
        assert not response.isError(), response
        return float(struct.unpack(">f", struct.pack(">HH", *response.registers))[0])
    finally:
        client.close()


def _terminal_request(request_id: str) -> dict[str, Any]:
    """通过 Commander 正式 CLI 查询终态，不自行推断执行结果。"""
    for _ in range(150):
        result = _cli("commander", "request-result", request_id, check=False)
        data = result.get("data", {})
        if data.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}:
            return data
        time.sleep(0.2)
    raise AssertionError(f"request {request_id} did not become terminal")


def _assert_applied(command_id: str, generation_id: str) -> int:
    token = os.environ["WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN"]
    channel = grpc.insecure_channel("127.0.0.1:52161")
    try:
        result = dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
            dg.GetInputExecutionRequest(
                generation_id=generation_id,
                request_id=command_id,
            ),
            metadata=(("authorization", f"Bearer {token}"),),
            timeout=10,
        )
    finally:
        channel.close()
    assert result.request_id == command_id
    assert result.state == "APPLIED", result
    assert result.effective_step > 0
    return int(result.effective_step)


@pytest.mark.parametrize(
    ("register", "point", "input_value", "expected_si"),
    [
        (41557, "active_power_setpoint", 450.0, 450000.0),
        (41555, "reactive_power_setpoint", 125.0, 125000.0),
    ],
)
def test_fc16_wtg_setpoint_reaches_dg_in_si_units(
    register: int, point: str, input_value: float, expected_si: float
) -> None:
    """验证 CDAB 与 kW/kvar→W/var 转换发生于 Commander 提交前。"""
    root = Path(os.environ.get("DCEMS_CONTROL_ROOT", str(ROOT / "run/dcems-control-wtg")))
    generation_id = (root / "state/generation-id.txt").read_text(encoding="utf-8").strip()
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    before = {event.event_id for event in _events(token)}

    client = ModbusTcpClient("127.0.0.1", port=CONTROL_PORT, timeout=5)
    try:
        assert client.connect()
        response = client.write_registers(register, _words_cdab(input_value), device_id=1)
        assert not response.isError(), response
        back = client.read_holding_registers(register, count=2, device_id=1)
        assert not back.isError()
        assert back.registers == _words_cdab(input_value)
    finally:
        client.close()

    request_id = ""
    for _ in range(100):
        request_id = _request_id(before, token) or ""
        if request_id:
            break
        time.sleep(0.1)
    assert request_id, "no gateway write event observed"
    request = _terminal_request(request_id)
    assert request["state"] == "SUCCEEDED", request
    assert request["io_settled"] is True, request
    assert len(request["commands"]) == 1
    command_id = str(request["commands"][0]["command_id"])

    command = _cli("commander", "result", command_id)["data"]
    assert command["state"] == "SUCCEEDED"
    assert command["outcome"] == "CONFIRMED_SUCCESS"
    assert command["io_settled"] is True
    assert command["point_id"] == point
    assert _assert_applied(command_id, generation_id) > 0

    # Gateway2 接收的是归一化后的 SI 工程量，不是 Gateway1 的 kW/kvar。
    executor = ModbusTcpClient("127.0.0.1", port=15421, timeout=5)
    try:
        assert executor.connect()
        for _ in range(100):
            applied = executor.read_holding_registers(register, count=2, device_id=1)
            if not applied.isError() and applied.registers == _words_cdab(expected_si):
                break
            time.sleep(0.1)
        else:
            pytest.fail(
                f"Gateway2 {point} did not publish SI value {expected_si}"
            )
    finally:
        executor.close()

    # 真正的转换证据来自 Commander/DG 回执；不能把源端 450 当作 W。
    evidence = {
        "modbus_register": register,
        "source_value": input_value,
        "source_unit": "kilowatt" if point == "active_power_setpoint" else "kilovar",
        "expected_generator_value": expected_si,
        "request_id": request_id,
        "command_id": command_id,
        "dg_state": "APPLIED",
        "gateway2_si_readback": expected_si,
    }
    out = root / "state" / f"wtg-{point}-evidence.json"
    out.write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_wtg_physical_feedback_is_not_a_modbus_ack() -> None:
    """输出动态反馈须由 Gateway2 实际读取，不能仅凭 ACK 宣称物理响应。"""
    watts = _read_power(15421, 38)
    assert 0 <= watts <= 2_000_000
