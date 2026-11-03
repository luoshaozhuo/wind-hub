"""UC-12 单服务链路诊断 System Test（CommonService.Diagnose 分阶段证据）。"""

from __future__ import annotations

import grpc
import pytest

from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def test_diagnose_collector_stages(cli_env: object) -> None:
    """S01：collector 诊断返回各阶段证据，overall PASS → 0。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "Diagnose"),
        common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[
                common_pb2.DiagnosticStage(stage="connect", status="PASS"),
                common_pb2.DiagnosticStage(stage="protocol", status="PASS"),
            ],
        ),
    )
    result = cli_env.subprocess_invoke("diagnose", "commander", "d1")
    assert result.exit_code == 0, result.stdout

    collector = cli_env.subprocess_invoke("diagnose", "collector", "pipeline", "d1")
    assert collector.exit_code == 0, collector.stdout
    data = collector.json()["data"]
    assert data["overall_status"] == "PASS"


def test_diagnose_unavailable_exit3(cli_env: object) -> None:
    """S03：诊断通道不可达 → UNKNOWN/3，不得宣称 PASS。"""
    cli_env.server.fail(
        cli_env.server.method_path("CommonService", "Diagnose"),
        grpc.StatusCode.UNAVAILABLE,
        "down",
    )
    result = cli_env.subprocess_invoke("diagnose", "gateway", "g1")
    assert result.exit_code == 3, result.stdout
    assert result.json()["status"] == "UNKNOWN"


def test_diagnose_gi_invalid_argument_envelope_exit2(cli_env: object) -> None:
    """S03（参数）：越界 --ca 必须走统一信封 + 退出码 2，不得裸 traceback。"""
    result = cli_env.subprocess_invoke("diagnose", "collector", "gi", "d1", "--ca", "99999")
    assert result.exit_code == 2, result.stdout
    envelope = result.json()
    assert envelope["status"] == "FAILURE"
    assert envelope["error_code"] == "ValueError"
    assert "0..65535" in envelope["message"]
    assert not [c for c in cli_env.server.calls if c.path.endswith("/InterrogateDevice")]


def test_diagnose_sink_bad_service_envelope_exit2(cli_env: object) -> None:
    """参数：非法 --service 在统一信封内失败（2），且不发出任何 RPC。"""
    result = cli_env.subprocess_invoke("sink", "deliveries", "--service", "bogus")
    assert result.exit_code == 2, result.stdout
    assert result.json()["error_code"] == "ValueError"
    assert not [c for c in cli_env.server.calls if "/Deliveries" in c.path]
