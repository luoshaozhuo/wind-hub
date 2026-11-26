"""真实 Modbus 写入 → Commander → DG 执行因果验收。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest
from pymodbus.client import ModbusTcpClient

from contracts import commander_pb2 as commander  # type: ignore[import-untyped]
from contracts import commander_pb2_grpc as commander_rpc
from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from contracts.scalar_codec import encode_scalar  # type: ignore[import-untyped]
from tests.third_party.dc_ems.prepare_control_chain import (
    COMMANDER_GRPC,
    EXECUTION_GRPC,
    GENERATOR_GRPC,
    INGRESS_GRPC,
    INGRESS_PORT,
)

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "run" / "dcems-control"
EXECUTION_MODBUS = 15425
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"}
LIVE_STATE: dict[str, str] = {}


@pytest.fixture(scope="module", autouse=True)
def require_live() -> None:
    """禁止只靠离线配置冒充真实运行结果。"""
    if os.environ.get("DCEMS_CONTROL_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_CONTROL_LIVE=1 required")


def _base() -> Path:
    return Path(os.environ.get("DCEMS_CONTROL_ROOT", str(BASE)))


def _environment() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}",
        "WIND_HUB_DEPLOYMENT_ID": "dcems-control",
        "WIND_HUB_DEPLOYMENT_VERSION": "1.0.0",
    }


def _cli(*args: str, check: bool = True) -> dict[str, Any]:
    """所有 Commander 查询走正式 CLI；从不直接改数据库。"""
    finished = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.main",
            "--config",
            str(_base() / "state" / "cli.yaml"),
            *args,
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


def _events(token: str, filter_text: str = "protocol write event") -> list[common.Event]:
    """由 Gateway1 的真实受授权接口查询指定事件，不读取私有内存。"""
    channel = grpc.insecure_channel(f"127.0.0.1:{INGRESS_GRPC}")
    try:
        stub = common_rpc.CommonServiceStub(channel)
        result = stub.GetEvents(
            common.GetEventsRequest(page_size=200, filter=filter_text),
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
            return cast(str, event.message.split(marker, 1)[1].strip())
    return None


def _wait_request_id(previous: set[str], token: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        request_id = _request_id(previous, token)
        if request_id:
            return request_id
        time.sleep(0.1)
    raise AssertionError("Gateway1 emitted no protocol-write event")


def _wait_port(port: int, *, opened: bool, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                reachable = True
        except OSError:
            reachable = False
        if reachable == opened:
            return
        time.sleep(0.1)
    raise AssertionError(f"port {port} did not become {'open' if opened else 'closed'}")


def _terminal(request_id: str, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        receipt = _cli("commander", "request-result", request_id, check=False)
        last = receipt
        data = receipt.get("data")
        if isinstance(data, dict) and data.get("state") in TERMINAL_STATES:
            return cast(dict[str, Any], data)
        time.sleep(0.2)
    raise AssertionError(f"request {request_id} did not settle: {last}")


def _generation_id() -> str:
    value = (_base() / "state" / "generation-id.txt").read_text(encoding="utf-8").strip()
    assert value
    return value


def _stop_generation() -> None:
    _cli("data-generator", "stop", _generation_id(), "--yes", check=False)


def _start_generation() -> None:
    for _ in range(8):
        receipt = _cli("data-generator", "start", "e2e-plant-physical", "--yes", check=False)
        if receipt.get("status") == "SUCCESS":
            generation_id = str(receipt["data"]["generation_id"])
            assert generation_id
            (_base() / "state" / "generation-id.txt").write_text(generation_id, encoding="utf-8")
            return
        time.sleep(1)
    raise AssertionError("Data Generator scenario failed to start")


def _start_stack(*, allow_degraded_commander: bool = False) -> None:
    for name in ("gateway", "commander", "gateway-target", "data-generator"):
        _cli("service", "start", name, "--yes", check=False)
    _start_generation()
    for port in (INGRESS_PORT, EXECUTION_MODBUS, COMMANDER_GRPC, EXECUTION_GRPC, GENERATOR_GRPC):
        _wait_port(port, opened=True)
    _wait_healthy(allow_degraded_commander=allow_degraded_commander)


def _stop_stack() -> None:
    _stop_generation()
    for name in ("data-generator", "gateway-target", "commander", "gateway"):
        _cli("service", "stop", name, "--yes", check=False)


def _wait_healthy(timeout: float = 20.0, *, allow_degraded_commander: bool = False) -> None:
    deadline = time.monotonic() + timeout
    pending: Any = {}
    while time.monotonic() < deadline:
        pending = _cli("service", "status", check=False)["data"]

        def ready(item: dict[str, Any]) -> bool:
            if item["process"]["state"] != "RUNNING":
                return False
            health = str(item["business"]["health"])
            if item["service"] == "commander" and allow_degraded_commander:
                return health in {"HEALTHY", "DEGRADED"}
            return health == "HEALTHY"

        if all(ready(item) for item in pending):
            return
        time.sleep(0.5)
    raise AssertionError(f"services not READY: {pending}")


def _service_pids() -> list[str]:
    finished = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False
    )
    return [
        line.strip()
        for line in finished.stdout.splitlines()
        if "run/dcems-control/configs" in line and "ps -eo" not in line
    ]


def _revision() -> str:
    return str(_cli("commander", "device", "PPC_SIM01")["data"]["device"]["revision"])


def _timestamp(seconds: float) -> Any:
    from google.protobuf.timestamp_pb2 import Timestamp

    stamp = Timestamp()
    stamp.FromDatetime(datetime.now(UTC) + timedelta(seconds=seconds))
    return stamp


def _modbus_write(value: int) -> int:
    client = ModbusTcpClient("127.0.0.1", port=INGRESS_PORT, timeout=5)
    try:
        assert client.connect(), "Gateway1 Modbus TCP unavailable"
        response = client.write_register(0, value, device_id=1)
        assert not response.isError(), response
        readback = client.read_holding_registers(0, count=1, device_id=1)
        assert not readback.isError()
        return int(readback.registers[0])
    finally:
        client.close()


def _execution_state(command_id: str) -> str:
    channel = grpc.insecure_channel(f"127.0.0.1:{GENERATOR_GRPC}")
    try:
        result = dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
            dg.GetInputExecutionRequest(generation_id=_generation_id(), request_id=command_id),
            metadata=(
                ("authorization", f"Bearer {os.environ['WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN']}"),
            ),
            timeout=10,
        )
        return str(result.state)
    except grpc.RpcError:
        return "UNAVAILABLE"
    finally:
        channel.close()


def _assert_rpc_error(call: Any, codes: set[grpc.StatusCode]) -> None:
    with pytest.raises(grpc.RpcError) as raised:
        call()
    assert raised.value.code() in codes, raised.value


def test_modbus_to_commander_to_generator_applied() -> None:
    """验证整个真实控制链和独立 Generator 证据。"""
    generation_id = _generation_id()
    operator = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    before = {event.event_id for event in _events(operator)}
    assert _modbus_write(1) == 1

    request_id = _wait_request_id(before, operator)

    command = _terminal(request_id)
    assert command["request_id"] == request_id
    assert command["state"] == "SUCCEEDED", command
    assert command["io_settled"] is True, command
    assert len(command["commands"]) == 1
    command_id = str(command["commands"][0]["command_id"])
    assert command_id
    completed = _cli("commander", "result", command_id)["data"]
    assert completed["state"] == "SUCCEEDED", completed
    assert completed["outcome"] == "CONFIRMED_SUCCESS", completed
    assert completed["io_settled"] is True, completed

    generator_token = os.environ["WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN"]
    channel = grpc.insecure_channel(f"127.0.0.1:{GENERATOR_GRPC}")
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
    executor = ModbusTcpClient("127.0.0.1", port=EXECUTION_MODBUS, timeout=5)
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
    LIVE_STATE["success_request_id"] = request_id
    LIVE_STATE["success_command_id"] = command_id
    evidence = {
        "modbus_ack": True,
        "request_id": request_id,
        "command_id": command_id,
        "commander_state": command["state"],
        "commander_outcome": completed["outcome"],
        "generator_state": execution.state,
        "effective_step": execution.effective_step,
        "gateway2_register": 1,
        "physical_plant_response_verified": False,
    }
    (_base() / "state" / "control-evidence.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )


def test_wrong_bearer_cannot_query_gateway1() -> None:
    """非法令牌不得访问 Gateway1 管理事件；Modbus 本身不具有 Bearer 认证。"""
    channel = grpc.insecure_channel(f"127.0.0.1:{INGRESS_GRPC}")
    try:
        stub = common_rpc.CommonServiceStub(channel)
        _assert_rpc_error(
            lambda: stub.GetEvents(
                common.GetEventsRequest(page_size=10),
                metadata=(("authorization", "Bearer invalid-" + "x" * 40),),
                timeout=5,
            ),
            {grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED},
        )
        _assert_rpc_error(
            lambda: stub.GetEvents(common.GetEventsRequest(page_size=10), timeout=5),
            {grpc.StatusCode.UNAUTHENTICATED},
        )
    finally:
        channel.close()


def test_commander_request_idempotency_and_conflict() -> None:
    """重复 request_id 仅复用原命令，不允许同键变更控制值。"""
    revision = _revision()
    request_id = f"dcems-control-{uuid4().hex}"
    expiry = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    base_args = (
        "commander",
        "write",
        "PPC_SIM01",
        "agc_enable",
        "1",
        "--expected-revision",
        str(revision),
        "--expires-at",
        expiry,
        "--request-id",
        request_id,
        "--yes",
    )
    first = _cli(*base_args)["data"]
    assert first["accepted"] is True
    replay = _cli(*base_args)["data"]
    assert replay["command_id"] == first["command_id"]
    conflict_args = list(base_args)
    conflict_args[4] = "0"
    conflict = _cli(*conflict_args, check=False)
    assert conflict["status"] == "FAILURE"
    assert conflict["error_code"] == "ALREADY_EXISTS"
    LIVE_STATE["idempotent_command_id"] = str(first["command_id"])


def test_commander_auth_and_control_permission() -> None:
    """Commander 拒绝非法/缺失 Bearer，并拒绝无 control 权限的身份。"""
    revision = _revision()
    request_id = f"dcems-control-{uuid4().hex}"
    submit = commander.SubmitCommandRequest(
        device_id="PPC_SIM01",
        point_id="agc_enable",
        value=encode_scalar(1),
        expires_at=_timestamp(30),
        expected_revision=revision,
        request_id=request_id,
    )
    channel = grpc.insecure_channel(f"127.0.0.1:{COMMANDER_GRPC}")
    try:
        stub = commander_rpc.CommanderServiceStub(channel)
        _assert_rpc_error(
            lambda: stub.SubmitCommand(
                submit,
                metadata=(("authorization", "Bearer invalid-" + "x" * 40),),
                timeout=5,
            ),
            {grpc.StatusCode.UNAUTHENTICATED},
        )
        _assert_rpc_error(
            lambda: stub.SubmitCommand(submit, timeout=5),
            {grpc.StatusCode.UNAUTHENTICATED},
        )
        maintenance = (
            ("authorization", f"Bearer {os.environ['WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN']}"),
        )
        _assert_rpc_error(
            lambda: stub.SubmitCommand(submit, metadata=maintenance, timeout=5),
            {grpc.StatusCode.PERMISSION_DENIED},
        )
        # 同一维护身份具有 query 权限，证明拒绝来自权限而非身份无效。
        device = stub.GetDevice(common.ObjectId(id="PPC_SIM01"), metadata=maintenance, timeout=5)
        assert device.device.device_id == "PPC_SIM01"
    finally:
        channel.close()
    # 被拒绝的请求没有产生任何命令身份。
    receipt = _cli("commander", "request-result", request_id, check=False)
    assert receipt["status"] == "FAILURE", receipt
    assert receipt["error_code"] == "NOT_FOUND", receipt


def test_gateway2_simulation_execute_permission() -> None:
    """仅 simulation_execute 身份可执行仿真；无权限身份不产生执行。"""
    revision = _revision()
    request_id = f"dcems-control-{uuid4().hex}"
    request = gateway.SimulationCommandRequest(
        gateway_id="gw-ppc-sim01",
        device_id="PPC_SIM01",
        point_id="agc_enable",
        value=encode_scalar(1),
        request_id=request_id,
        expected_revision=revision,
        expires_at=_timestamp(30),
    )
    operator = os.environ["WIND_HUB_E2E_GATEWAY_OPERATOR_TOKEN"]
    channel = grpc.insecure_channel(f"127.0.0.1:{EXECUTION_GRPC}")
    try:
        stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
        _assert_rpc_error(
            lambda: stub.ExecuteSimulationCommand(
                request,
                metadata=(("authorization", f"Bearer {operator}"),),
                timeout=5,
            ),
            {grpc.StatusCode.PERMISSION_DENIED},
        )
        _assert_rpc_error(
            lambda: stub.ExecuteSimulationCommand(request, timeout=5),
            {grpc.StatusCode.UNAUTHENTICATED},
        )
        # 查询同样受保护：无 simulation_execute 身份无法读取执行证据。
        _assert_rpc_error(
            lambda: stub.GetSimulationExecutionResult(
                gateway.SimulationExecutionQuery(gateway_id="gw-ppc-sim01", request_id=request_id),
                metadata=(("authorization", f"Bearer {operator}"),),
                timeout=5,
            ),
            {grpc.StatusCode.PERMISSION_DENIED},
        )
        commander_token = os.environ["WIND_HUB_E2E_COMMANDER_GATEWAY_TOKEN"]
        result = stub.GetSimulationExecutionResult(
            gateway.SimulationExecutionQuery(gateway_id="gw-ppc-sim01", request_id=request_id),
            metadata=(("authorization", f"Bearer {commander_token}"),),
            timeout=5,
        )
        assert result.state != "APPLIED"
    finally:
        channel.close()


def test_generator_update_requires_update_permission() -> None:
    """DG 只读身份不得更新控制输入，且不产生执行证据。"""
    request_id = f"dcems-control-{uuid4().hex}"
    request = dg.UpdateVariablesRequest(
        generation_id=_generation_id(),
        device_id="PPC_SIM01",
        request_id=request_id,
        values=[dg.VariableUpdate(point_id="agc_enable", value=encode_scalar(0))],
    )
    maintenance = os.environ["WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN"]
    gateway_token = os.environ["WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN"]
    channel = grpc.insecure_channel(f"127.0.0.1:{GENERATOR_GRPC}")
    try:
        stub = dg_rpc.DataGeneratorServiceStub(channel)
        _assert_rpc_error(
            lambda: stub.UpdateVariables(
                request,
                metadata=(("authorization", f"Bearer {maintenance}"),),
                timeout=5,
            ),
            {grpc.StatusCode.PERMISSION_DENIED},
        )
        _assert_rpc_error(
            lambda: stub.UpdateVariables(request, timeout=5),
            {grpc.StatusCode.UNAUTHENTICATED},
        )
        _assert_rpc_error(
            lambda: stub.UpdateVariables(
                request,
                metadata=(("authorization", "Bearer invalid-" + "x" * 40),),
                timeout=5,
            ),
            {grpc.StatusCode.UNAUTHENTICATED},
        )
        execution = stub.GetInputExecution(
            dg.GetInputExecutionRequest(generation_id=_generation_id(), request_id=request_id),
            metadata=(("authorization", f"Bearer {gateway_token}"),),
            timeout=5,
        )
        assert execution.state != "APPLIED"
    finally:
        channel.close()


def test_commander_request_query_is_caller_scoped() -> None:
    """request_id 查询严格按 caller_id 隔离，跨身份不可见。"""
    request_id = LIVE_STATE["success_request_id"]
    token = os.environ["WIND_HUB_E2E_COMMANDER_MAINTENANCE_TOKEN"]
    channel = grpc.insecure_channel(f"127.0.0.1:{COMMANDER_GRPC}")
    try:
        stub = commander_rpc.CommanderServiceStub(channel)
        _assert_rpc_error(
            lambda: stub.GetRequestResult(
                commander.GetRequestResultRequest(request_id=request_id),
                metadata=(("authorization", f"Bearer {token}"),),
                timeout=5,
            ),
            {grpc.StatusCode.NOT_FOUND},
        )
    finally:
        channel.close()


def test_commander_unreachable_never_forges_success() -> None:
    """Commander 失联时 Gateway1 记录未成功转发，且不产生命令或执行。"""
    operator = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    _cli("service", "stop", "commander", "--yes")
    _wait_port(COMMANDER_GRPC, opened=False)
    time.sleep(2.0)  # 让 Gateway1 的 revision 缓存过期。
    before = {event.event_id for event in _events(operator)}
    assert _modbus_write(0) == 0
    request_id = _wait_request_id(before, operator)

    forward = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for event in _events(operator, "forward"):
            if request_id in event.message and event.message.startswith("forward "):
                forward = event.message
                break
        if forward:
            break
        time.sleep(0.2)
    assert forward, "Gateway1 did not record a forwarding outcome"
    assert not forward.startswith("forward ACCEPTED"), forward
    assert forward.startswith(
        ("forward REJECTED", "forward UNKNOWN", "forward OVERLOADED")
    ), forward

    _cli("service", "start", "commander", "--yes")
    _wait_port(COMMANDER_GRPC, opened=True)
    deadline = time.monotonic() + 10
    receipt: dict[str, Any] = {}
    while time.monotonic() < deadline:
        receipt = _cli("commander", "request-result", request_id, check=False)
        if receipt.get("status") == "FAILURE":
            break
        time.sleep(0.2)
    assert receipt["status"] == "FAILURE", receipt
    assert receipt["error_code"] == "NOT_FOUND", receipt
    assert _execution_state(request_id) != "APPLIED"
    LIVE_STATE["commander_unreachable_request"] = request_id


def test_gateway2_unreachable_is_terminal_and_not_retried() -> None:
    """Gateway2 不可达时命令不得伪报成功，恢复后也不自动重放。"""
    revision = _revision()
    _cli("service", "stop", "gateway", "--yes")
    _wait_port(EXECUTION_GRPC, opened=False)
    _wait_port(EXECUTION_MODBUS, opened=False)
    request_id = f"dcems-control-{uuid4().hex}"
    receipt = _cli(
        "commander",
        "write",
        "PPC_SIM01",
        "agc_enable",
        "1",
        "--expected-revision",
        revision,
        "--expires-at",
        (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        "--request-id",
        request_id,
        "--yes",
    )["data"]
    assert receipt["accepted"] is True
    command = _terminal(request_id)
    assert command["state"] != "SUCCEEDED", command
    assert command["commands"][0]["outcome"] != "CONFIRMED_SUCCESS", command
    assert len(command["commands"]) == 1
    command_id = str(command["commands"][0]["command_id"])
    assert _execution_state(command_id) != "APPLIED"

    _cli("service", "start", "gateway", "--yes")
    _wait_port(EXECUTION_GRPC, opened=True)
    _wait_port(EXECUTION_MODBUS, opened=True)
    time.sleep(2.0)
    replay = _cli("commander", "request-result", request_id, check=False)["data"]
    assert replay["state"] == command["state"], replay
    assert replay["commands"][0]["outcome"] == command["commands"][0]["outcome"], replay
    assert len(replay["commands"]) == 1, replay
    assert _execution_state(command_id) != "APPLIED"
    LIVE_STATE["gateway2_unreachable_command"] = command_id
    LIVE_STATE["gateway2_unreachable_state"] = str(command["state"])


def test_generator_unreachable_is_unknown_and_not_replayed() -> None:
    """DG 不可达时不得伪报成功；恢复后旧 UNKNOWN 不得被重放为 APPLIED。"""
    revision = _revision()
    _stop_generation()
    _cli("service", "stop", "data-generator", "--yes")
    _wait_port(GENERATOR_GRPC, opened=False)
    request_id = f"dcems-control-{uuid4().hex}"
    receipt = _cli(
        "commander",
        "write",
        "PPC_SIM01",
        "agc_enable",
        "1",
        "--expected-revision",
        revision,
        "--expires-at",
        (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        "--request-id",
        request_id,
        "--yes",
    )["data"]
    assert receipt["accepted"] is True
    command = _terminal(request_id)
    assert command["state"] != "SUCCEEDED", command
    assert command["commands"][0]["outcome"] != "CONFIRMED_SUCCESS", command
    assert len(command["commands"]) == 1
    command_id = str(command["commands"][0]["command_id"])
    assert _execution_state(command_id) != "APPLIED"
    assert command["commands"][0]["state"] == "UNKNOWN", command

    _cli("service", "start", "data-generator", "--yes")
    _wait_port(GENERATOR_GRPC, opened=True)
    _start_generation()
    time.sleep(2.0)
    replay = _cli("commander", "request-result", request_id, check=False)["data"]
    assert replay["state"] == "UNKNOWN", replay
    assert len(replay["commands"]) == 1, replay
    assert _execution_state(command_id) != "APPLIED"
    LIVE_STATE["generator_unreachable_command"] = command_id


def test_result_query_failure_then_recovery() -> None:
    """查询通道失联时保留证据，恢复后仍可查询既有终态。"""
    command_id = LIVE_STATE["success_command_id"]
    _cli("service", "stop", "commander", "--yes")
    _wait_port(COMMANDER_GRPC, opened=False)
    failed = _cli("commander", "result", command_id, check=False)
    assert failed.get("status") != "SUCCESS", failed
    assert failed.get("data") is None, failed
    _cli("service", "start", "commander", "--yes")
    _wait_port(COMMANDER_GRPC, opened=True)
    deadline = time.monotonic() + 10
    recovered: dict[str, Any] = {}
    while time.monotonic() < deadline:
        receipt = _cli("commander", "result", command_id, check=False)
        if receipt.get("status") == "SUCCESS":
            recovered = cast(dict[str, Any], receipt["data"])
            break
        time.sleep(0.2)
    assert recovered["state"] == "SUCCEEDED", recovered
    assert recovered["outcome"] == "CONFIRMED_SUCCESS", recovered


def test_stop_restart_preserves_state_and_releases_resources() -> None:
    """正式 CLI 完整停机/重启：端口与进程释放，数据库状态保持。"""
    success_id = LIVE_STATE["success_command_id"]
    unknown_id = LIVE_STATE["gateway2_unreachable_command"]
    before = _cli("commander", "result", success_id)["data"]
    _stop_stack()
    status = _cli("service", "status", check=False)["data"]
    assert all(item["process"]["state"] == "STOPPED" for item in status), status
    for port in (INGRESS_PORT, EXECUTION_MODBUS, COMMANDER_GRPC, EXECUTION_GRPC, GENERATOR_GRPC):
        _wait_port(port, opened=False)
    assert not _service_pids(), _service_pids()

    _start_stack(allow_degraded_commander=True)
    after = _cli("commander", "result", success_id)["data"]
    assert after["state"] == before["state"] == "SUCCEEDED", after
    assert after["outcome"] == before["outcome"] == "CONFIRMED_SUCCESS", after
    retained = _cli("commander", "result", unknown_id, check=False)["data"]
    assert retained["state"] == LIVE_STATE["gateway2_unreachable_state"], retained
    assert retained["state"] != "SUCCEEDED", retained
    uncertain = _cli(
        "commander", "result", LIVE_STATE["generator_unreachable_command"], check=False
    )["data"]
    assert uncertain["state"] == "UNKNOWN", uncertain
    assert uncertain["outcome"] == "UNKNOWN", uncertain
    status = _cli("service", "status", check=False)["data"]
    commander = next(item for item in status if item["service"] == "commander")
    assert commander["business"]["health"] == "DEGRADED", commander
