"""真实 Modbus 写入 → Commander → DG 执行因果验收。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import grpc
import pytest
from pymodbus.client import ModbusTcpClient

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from tests.third_party.dc_ems.prepare_control_chain import INGRESS_PORT

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "run" / "dcems-control"


@pytest.fixture(scope="module", autouse=True)
def require_live() -> None:
    """禁止只靠离线配置冒充真实运行结果。"""
    if os.environ.get("DCEMS_CONTROL_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_CONTROL_LIVE=1 required")


def _environment() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}",
        "WIND_HUB_DEPLOYMENT_ID": "dcems-control",
        "WIND_HUB_DEPLOYMENT_VERSION": "1.0.0",
    }


def _cli(*args: str, check: bool = True) -> dict[str, Any]:
    """所有 Commander 查询走正式 CLI；从不直接改数据库。"""
    base = Path(os.environ.get("DCEMS_CONTROL_ROOT", str(BASE)))
    finished = subprocess.run(
        [
            sys.executable, "-m", "cli.main", "--config",
            str(base / "state" / "cli.yaml"), *args,
        ],
        env=_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if check:
        assert finished.returncode == 0, (args, finished.stdout, finished.stderr)
    return cast(dict[str, Any], json.loads(finished.stdout))


def _events(token: str) -> list[common.Event]:
    """由 Gateway1 的真实受授权接口查询写入事件，不读取私有内存。"""
    channel = grpc.insecure_channel("127.0.0.1:52174")
    try:
        stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
        result = stub.GetEvents(
            common.GetEventsRequest(page_size=200, filter="protocol write event"),
            metadata=(("authorization", f"Bearer {token}"),),
            timeout=5,
        )
        return list(result.events)
    finally:
        channel.close()


def _request_id(previous: set[str], token: str) -> str | None:
    """获得本次写入引发的事件 ID，此 ID 正是 Commander request_id。"""
    for event in _events(token):
        if event.event_id in previous:
            continue
        marker = "protocol write event "
        if marker in event.message:
            return event.message.split(marker, 1)[1].strip()
    return None


def test_modbus_to_commander_to_generator_applied() -> None:
    """验证整个真实控制链和独立 Generator 证据。"""
    base = Path(os.environ.get("DCEMS_CONTROL_ROOT", str(BASE)))
    generation_id = (base / "state" / "generation-id.txt").read_text(encoding="utf-8").strip()
    assert generation_id
    operator = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    before = {event.event_id for event in _events(operator)}
    client = ModbusTcpClient("127.0.0.1", port=INGRESS_PORT, timeout=5)
    try:
        assert client.connect(), "Gateway1 Modbus TCP unavailable"
        response = client.write_register(0, 1, device_id=1)
        assert not response.isError(), response
        readback = client.read_holding_registers(0, count=1, device_id=1)
        assert not readback.isError()
        assert readback.registers == [1]
    finally:
        client.close()

    request_id = ""
    for _ in range(100):
        request_id = _request_id(before, operator) or ""
        if request_id:
            break
        time.sleep(0.1)
    assert request_id, "Gateway1 emitted no protocol-write event"

    command: dict[str, Any] | None = None
    for _ in range(150):
        receipt = _cli("commander", "request-result", request_id, check=False)
        if receipt.get("status") == "SUCCESS":
            data = receipt["data"]
            if data.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}:
                command = data
                break
        time.sleep(0.2)
    assert command is not None, "Commander request did not reach terminal state"
    assert command["request_id"] == request_id
    assert command["state"] == "SUCCEEDED", command
    assert command["outcome"] == "CONFIRMED_SUCCESS", command
    assert command["io_settled"] is True, command
    assert len(command["commands"]) == 1
    command_id = str(command["commands"][0]["command_id"])
    assert command_id

    generator_token = os.environ["WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN"]
    channel = grpc.insecure_channel("127.0.0.1:52161")
    try:
        execution = dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
            dg.GetInputExecutionRequest(generation_id=generation_id, request_id=command_id),
            metadata=(("authorization", f"Bearer {generator_token}"),),
            timeout=10,
        )
    finally:
        channel.close()
    assert execution.request_id == command_id
    assert execution.state == "APPLIED"
    assert execution.effective_step > 0

    # Gateway2 控制回读是额外证据，不能取代 DG 的实际执行状态。
    executor = ModbusTcpClient("127.0.0.1", port=15425, timeout=5)
    try:
        assert executor.connect(), "Gateway2 PPC listener unavailable"
        for _ in range(50):
            feedback = executor.read_holding_registers(0, count=1, device_id=1)
            if not feedback.isError() and feedback.registers == [1]:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Gateway2 did not publish applied PPC control input")
    finally:
        executor.close()
    evidence = {
        "modbus_ack": True,
        "request_id": request_id,
        "command_id": command_id,
        "commander_state": command["state"],
        "commander_outcome": command["outcome"],
        "generator_state": execution.state,
        "effective_step": execution.effective_step,
        "gateway2_register": 1,
        "physical_plant_response_verified": False,
    }
    target = base / "state" / "control-evidence.json"
    target.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
