"""真实 DCEMS Server/Web 控制输出通过 Modbus FC16 到 DG 的验收。"""

from __future__ import annotations

import base64
import json
import os
import ssl
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import grpc
import pytest
from pymodbus.client import ModbusTcpClient

from contracts import common_pb2 as common  # type: ignore[import-untyped]
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from tests.third_party.dc_ems.test_control_chain_live import _cli, _events, _request_id, _wait_port
from tests.third_party.dc_ems.test_wtg_control_chain_live import (
    _assert_applied,
    _read_power,
    _terminal_request,
    _words_cdab,
)

ROOT = Path(__file__).resolve().parents[3]
SERVER_URL = os.environ.get("DCEMS_WTG_SERVER_URL", "https://127.0.0.1:18453")
CONTROL_DEVICE_ID = "WTG01"
POWER_POINT = "ctrlActPow"
REACTIVE_POINT = "ctrlReActPow"


@pytest.fixture(scope="module", autouse=True)
def require_server() -> None:
    """显式授权真实 DCEMS Server 工业控制测试，严禁误连接生产环境。"""
    if os.environ.get("DCEMS_WTG_DCEMS_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_WTG_DCEMS_LIVE=1 required")
    if os.environ.get("DCEMS_ALLOW_LOCAL_SMOKE") != "1":
        pytest.skip("NOT_RUN: isolated DCEMS Server opt-in required")


def _base() -> Path:
    return Path(os.environ.get("DCEMS_CONTROL_ROOT", str(ROOT / "run" / "dcems-control-wtg")))


def _generation_id() -> str:
    return (_base() / "state" / "generation-id.txt").read_text(encoding="utf-8").strip()


def _ingress_listener(action: str) -> None:
    """Use the authorized Gateway1 management RPC so event API stays available."""
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    channel = grpc.insecure_channel("127.0.0.1:52174")
    try:
        stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
        revision = stub.GetGateway(
            common.ObjectId(id="dcems-control-ingress-wtg"),
            metadata=(("authorization", f"Bearer {token}"),),
            timeout=10,
        ).gateway.revision
        assert revision
        result = getattr(stub, action)(
            common.OperationRequest(id="dcems-control-ingress-wtg", expected_revision=revision),
            metadata=(("authorization", f"Bearer {token}"),),
            timeout=10,
        )
        assert result.accepted, result
    finally:
        channel.close()


def _ssl_context() -> ssl.SSLContext:
    """The released Server serves HTTPS with a site-generated self-signed JKS."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _request(
    path: str, *, data: bytes | None = None, token: str | None = None, method: str = "GET"
) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["token"] = token
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{SERVER_URL}{path}", data=data, method=method, headers=headers)
    with urlopen(request, timeout=15, context=_ssl_context()) as response:
        return response.status, cast(dict[str, Any], json.loads(response.read(65536)))


def _credentials() -> tuple[str, str]:
    """Prefer explicit test env; otherwise use the isolated seed's published admin."""
    user = os.environ.get("DCEMS_WTG_WEB_USER")
    password = os.environ.get("DCEMS_WTG_WEB_PASSWORD")
    if user and password:
        return user, password
    seed = json.loads((_base() / "data.json").read_text(encoding="utf-8"))
    admin = next(item for item in seed["userInfos"] if item.get("userAcount") == "admin")
    return str(admin["userAcount"]), str(admin["passwd"])


def _login() -> str:
    """Authenticate against the real Server; return only the session token."""
    user, password = _credentials()
    stamp = str(int(time.time() * 1000))
    auth = base64.b64encode(f"{stamp}/{user}/{password}".encode()).decode("ascii")
    status, body = _request(f"/api/login?{urlencode({'auth': auth})}")
    assert status == 200, status
    assert body.get("code") == "200", body
    data = body.get("data")
    assert isinstance(data, dict) and data, "login returned no session token"
    return str(next(iter(data)))


def _set_param(token: str, values: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    payload = json.dumps({"param": values}, allow_nan=False).encode("utf-8")
    return _request("/api/config/setParam", data=payload, token=token, method="POST")


def _dcems_set_point(token: str, point: str, value: float) -> dict[str, Any]:
    """调用真实 DCEMS 控制参数入口；其成功回执不代表 Modbus 执行成功。"""
    status, body = _set_param(token, {f"{CONTROL_DEVICE_ID}_{point}": value})
    assert status == 200, status
    return body


def _wait_physical_power(expected_w: float, *, timeout: float = 35.0) -> float:
    """等待真实模型有功输出进入目标 ±1% 或 2 kW。"""
    tolerance = max(abs(expected_w) * 0.01, 2000.0)
    deadline = time.monotonic() + timeout
    samples: list[float] = []
    while time.monotonic() < deadline:
        samples.append(_read_power(15421, 38))
        recent = samples[-8:]
        if len(recent) == 8 and all(abs(item - expected_w) <= tolerance for item in recent):
            return sum(recent) / len(recent)
        time.sleep(0.5)
    pytest.fail(f"physical power did not settle to {expected_w} W: {samples[-8:]}")


def _assert_no_new_protocol_write(prior: set[str]) -> None:
    time.sleep(0.5)  # 允许异步转发/事件写入落定后再判定“未写入”。
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    assert _request_id(prior, token) is None, "rejected DCEMS call still reached Gateway1"


def test_real_dcems_server_wtg_power_command_reaches_dg() -> None:
    """真实 Server 登录→setParam→Modbus→Gateway1→Commander→Gateway2→DG。"""
    base = _base()
    generation_id = (base / "state" / "generation-id.txt").read_text(encoding="utf-8").strip()
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    prior = {event.event_id for event in _events(token)}
    session = _login()
    web_result = _dcems_set_point(session, POWER_POINT, 450.0)
    assert web_result.get("code") == "200", web_result

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
    assert len(request["commands"]) == 1, request
    command_id = str(request["commands"][0]["command_id"])
    command = _cli("commander", "result", command_id)["data"]
    assert command["device_id"] == "WTG_SIM01"
    assert command["point_id"] == "active_power_setpoint"
    assert command["outcome"] == "CONFIRMED_SUCCESS"
    assert _assert_applied(command_id, generation_id) > 0

    gateway2 = ModbusTcpClient("127.0.0.1", port=15421, timeout=5)
    try:
        assert gateway2.connect()
        for _ in range(100):
            actual = gateway2.read_holding_registers(41557, count=2, device_id=1)
            if not actual.isError() and actual.registers == _words_cdab(450000.0):
                break
            time.sleep(0.1)
        else:
            pytest.fail("Gateway2 did not publish 450000 W after DCEMS control")
    finally:
        gateway2.close()

    physical_w = _wait_physical_power(450000.0)
    evidence = {
        "source": "dc-ems Server /api/config/setParam",
        "dcems_device_id": CONTROL_DEVICE_ID,
        "windhub_device_id": "WTG_SIM01",
        "dcems_point_id": POWER_POINT,
        "commanded_kw": 450.0,
        "expected_dg_w": 450000.0,
        "gateway_event_id": event_id,
        "commander_command_id": command_id,
        "commander_outcome": command["outcome"],
        "generator_state": "APPLIED",
        "gateway2_readback_w": 450000.0,
        "physical_active_power_w": physical_w,
        "dcems_web_ack_is_not_execution_proof": True,
    }
    (base / "state" / "dcems-server-control-evidence.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )


def test_real_dcems_server_wtg_reactive_command_reaches_dg() -> None:
    """真实 Server 125 kvar→125000 var，经 FC16 到达 DG 与 Gateway2。"""
    base = _base()
    generation_id = (base / "state" / "generation-id.txt").read_text(encoding="utf-8").strip()
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    prior = {event.event_id for event in _events(token)}
    session = _login()
    web_result = _dcems_set_point(session, REACTIVE_POINT, 125.0)
    assert web_result.get("code") == "200", web_result

    event_id = ""
    for _ in range(150):
        event_id = _request_id(prior, token) or ""
        if event_id:
            break
        time.sleep(0.1)
    assert event_id, "DCEMS reactive control did not reach Gateway1"
    request = _terminal_request(event_id)
    assert request["state"] == "SUCCEEDED", request
    assert request["io_settled"] is True, request
    assert len(request["commands"]) == 1, request
    command_id = str(request["commands"][0]["command_id"])
    command = _cli("commander", "result", command_id)["data"]
    assert command["device_id"] == "WTG_SIM01"
    assert command["point_id"] == "reactive_power_setpoint"
    assert command["outcome"] == "CONFIRMED_SUCCESS"
    assert _assert_applied(command_id, generation_id) > 0

    gateway2 = ModbusTcpClient("127.0.0.1", port=15421, timeout=5)
    try:
        assert gateway2.connect()
        for _ in range(100):
            actual = gateway2.read_holding_registers(41555, count=2, device_id=1)
            if not actual.isError() and actual.registers == _words_cdab(125000.0):
                break
            time.sleep(0.1)
        else:
            pytest.fail("Gateway2 did not publish 125000 var after DCEMS control")
    finally:
        gateway2.close()

    evidence = {
        "source": "dc-ems Server /api/config/setParam",
        "dcems_device_id": CONTROL_DEVICE_ID,
        "windhub_device_id": "WTG_SIM01",
        "dcems_point_id": REACTIVE_POINT,
        "commanded_kvar": 125.0,
        "expected_dg_var": 125000.0,
        "gateway_event_id": event_id,
        "commander_command_id": command_id,
        "commander_outcome": command["outcome"],
        "generator_state": "APPLIED",
        "gateway2_readback_var": 125000.0,
        "dcems_web_ack_is_not_execution_proof": True,
    }
    (base / "state" / "dcems-server-reactive-evidence.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )


def test_dcems_gateway_disconnect_is_observable_and_recoverable() -> None:
    """Gateway1 失联时 Web 仍回“控制成功”但无 FC16；恢复后控制可用。"""
    session = _login()
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    _ingress_listener("StopGateway")
    _wait_port(15531, opened=False)
    prior = {event.event_id for event in _events(token)}
    body = _dcems_set_point(session, POWER_POINT, 450.0)
    assert body.get("code") == "200", body  # 源码限制：Web 忽略 getter.control 返回值。
    time.sleep(2.0)
    assert _request_id(prior, token) is None, "downed Gateway1 still received a protocol write"

    _ingress_listener("StartGateway")
    _wait_port(15531, opened=True)
    time.sleep(5.0)  # 等待 Server 重新完成 FC03 轮询并置位 comOk。
    prior = {event.event_id for event in _events(token)}
    body = _dcems_set_point(session, POWER_POINT, 450.0)
    assert body.get("code") == "200", body
    event_id = ""
    for _ in range(150):
        event_id = _request_id(prior, token) or ""
        if event_id:
            break
        time.sleep(0.1)
    assert event_id, "control did not recover after Gateway1 restart"
    request = _terminal_request(event_id)
    assert request["state"] == "SUCCEEDED", request
    assert len(request["commands"]) == 1, request
    command_id = str(request["commands"][0]["command_id"])
    command = _cli("commander", "result", command_id)["data"]
    assert command["outcome"] == "CONFIRMED_SUCCESS", command
    assert _assert_applied(command_id, _generation_id()) > 0


def test_dcems_login_rejects_wrong_password() -> None:
    """真实 Server 登录必须校验密码，不得接受任意会话。"""
    user, password = _credentials()
    stamp = str(int(time.time() * 1000))
    auth = base64.b64encode(f"{stamp}/{user}/{password}-wrong".encode()).decode("ascii")
    status, body = _request(f"/api/login?{urlencode({'auth': auth})}")
    assert status == 200
    assert body.get("code") != "200", "wrong password was accepted"


def test_dcems_control_requires_token() -> None:
    """无会话 token 的 setParam 必须被拒绝且不产生 Modbus 写入。"""
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    prior = {event.event_id for event in _events(token)}
    status, body = _request(
        "/api/config/setParam",
        data=json.dumps({"param": {f"{CONTROL_DEVICE_ID}_{POWER_POINT}": 450.0}}).encode(),
        method="POST",
    )
    assert status == 200
    assert body.get("code") == "401", body
    _assert_no_new_protocol_write(prior)


def test_dcems_rejects_out_of_range_power() -> None:
    """超过设备容量的设定值必须在 Server 入口拒绝，不得写向 Gateway。"""
    session = _login()
    token = os.environ["WIND_HUB_DCE_CONTROL_INGRESS_OPERATOR_TOKEN"]
    prior = {event.event_id for event in _events(token)}
    status, body = _set_param(session, {f"{CONTROL_DEVICE_ID}_{POWER_POINT}": 3000.0})
    assert status == 200
    assert body.get("code") == "400", body
    _assert_no_new_protocol_write(prior)
