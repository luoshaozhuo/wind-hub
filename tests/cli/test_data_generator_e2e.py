"""UC-11 Data Generator 查询与控制 System Test。"""

from __future__ import annotations

from typing import Any

import pytest

from contracts import common_pb2
from contracts import data_generator_pb2 as dg

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _p(server: Any, method: str) -> str:
    return server.method_path("DataGeneratorService", method)


def test_generator_devices_scenarios_roundtrip(cli_env: Any) -> None:
    """S01：设备列表、场景列表（available_versions 真实序列化）。"""
    server = cli_env.server
    server.respond_value(
        _p(server, "ListDevices"),
        dg.ListDevicesResponse(devices=[dg.Device(device_id="gen1")]),
    )
    server.respond_value(
        _p(server, "ListScenarios"),
        dg.ListScenariosResponse(
            scenarios=[
                dg.Scenario(scenario_id="sc1", active_version="v2", available_versions=["v1", "v2"])
            ]
        ),
    )
    r1 = cli_env.subprocess_invoke("data-generator", "devices")
    assert r1.exit_code == 0, r1.stdout
    assert r1.json()["data"]["devices"][0]["device_id"] == "gen1"

    r2 = cli_env.subprocess_invoke("data-generator", "scenarios")
    assert r2.exit_code == 0, r2.stdout
    scenario = r2.json()["data"]["scenarios"][0]
    assert scenario["available_versions"] == ["v1", "v2"]


def test_generator_start_accept_stop_status(cli_env: Any) -> None:
    """S01/S02：start 受理（accepted）、status 查询、stop；revision 来自配置快照。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(revision="cfg-r"),
    )
    server.respond_value(
        _p(server, "StartGeneration"),
        dg.StartGenerationResponse(generation_id="g1", accepted=True, actual_state="ACCEPTED"),
    )
    server.respond_value(
        _p(server, "GetGeneration"),
        dg.GetGenerationResponse(generation=dg.Generation(generation_id="g1")),
    )
    server.respond_value(_p(server, "StopGeneration"), dg.StopGenerationResponse(accepted=True))

    started = cli_env.subprocess_invoke(
        "data-generator", "start", "sc1", "--scenario-version", "v2", "--yes"
    )
    assert started.exit_code == 0, started.stdout
    start_call = [c for c in server.calls if c.path.endswith("/StartGeneration")][0]
    assert start_call.request.scenario_id == "sc1"
    assert start_call.request.scenario_version == "v2"
    assert start_call.request.expected_revision == "cfg-r"

    status = cli_env.subprocess_invoke("data-generator", "status", "g1")
    assert status.exit_code == 0, status.stdout

    stopped = cli_env.subprocess_invoke(
        "data-generator", "stop", "g1", "--expected-revision", "cfg-r", "--yes"
    )
    assert stopped.exit_code == 0, stopped.stdout


def test_generator_start_rejected_exit1(cli_env: Any) -> None:
    """S03：服务端拒绝受理（accepted=False）必须按失败上报，不得当成成功。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(revision="cfg-r"),
    )
    server.respond_value(
        _p(server, "StartGeneration"),
        dg.StartGenerationResponse(accepted=False, error_code="BUSY", message="running"),
    )
    result = cli_env.subprocess_invoke("data-generator", "start", "sc1", "--yes")
    assert result.exit_code == 1, result.stdout


def test_generator_variables_list_and_update(cli_env: Any) -> None:
    """模型输入目录与受控更新：list 分页读回、update 受理与非可写拒绝。"""
    server = cli_env.server
    writable = dg.ModelVariable(
        point_id="wind_speed_setpoint",
        business_point_id="wind_speed",
        # 可写输入支持的编码类型（float32/float64/bool/str/int）。
        data_type="float64",
        unit="meter_per_second",
        role="input",
        writable=True,
        minimum=0.0,
        maximum=25.0,
    )
    frozen = dg.ModelVariable(
        point_id="rotor_speed",
        data_type="float64",
        unit="rpm",
        role="output",
        writable=False,
    )
    server.respond_value(
        _p(server, "ListVariables"),
        dg.ListVariablesResponse(
            variables=[writable, frozen],
            point_table_version="1.0.0",
            generation_state="RUNNING",
        ),
    )
    listed = cli_env.subprocess_invoke(
        "data-generator", "variables", "list", "--generation-id", "g1", "--device-id", "gen1"
    )
    assert listed.exit_code == 0, listed.stdout
    data = listed.json()["data"]
    assert [item["point_id"] for item in data["variables"]] == [
        "wind_speed_setpoint",
        "rotor_speed",
    ]
    assert data["point_table_version"] == "1.0.0"

    server.respond_value(
        _p(server, "UpdateVariables"),
        dg.UpdateVariablesResponse(accepted=True, effective_step=7, request_id="req-1"),
    )
    updated = cli_env.subprocess_invoke(
        "data-generator",
        "variables",
        "update",
        "--generation-id",
        "g1",
        "--device-id",
        "gen1",
        "--request-id",
        "req-1",
        "--point-id",
        "wind_speed_setpoint",
        "--value",
        "11.5",
        "--yes",
    )
    assert updated.exit_code == 0, updated.stdout
    update_data = updated.json()["data"]
    assert update_data["accepted"] is True
    assert int(update_data["effective_step"]) == 7

    # 非 writable 输入：统一信封失败（2），且不发出 UpdateVariables。
    rejected = cli_env.subprocess_invoke(
        "data-generator",
        "variables",
        "update",
        "--generation-id",
        "g1",
        "--device-id",
        "gen1",
        "--request-id",
        "req-2",
        "--point-id",
        "rotor_speed",
        "--value",
        "12.0",
        "--yes",
    )
    assert rejected.exit_code == 2, rejected.stdout
    assert rejected.json()["status"] == "FAILURE"
    update_calls = [c for c in server.calls if c.path.endswith("/UpdateVariables")]
    assert len(update_calls) == 1, "non-writable input must not reach UpdateVariables"
