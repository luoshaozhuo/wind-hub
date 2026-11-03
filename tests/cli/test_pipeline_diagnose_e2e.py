"""UC-13 跨服务数据链路诊断 System Test：无关联证据不得宣称端到端 PASS。"""

from __future__ import annotations

from typing import Any

import pytest

from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _stage(stage: str, status: str, evidence: list[tuple[str, str]] | None = None) -> Any:
    return common_pb2.DiagnosticStage(
        stage=stage,
        status=status,
        evidence=[common_pb2.Evidence(name=n, value=v) for n, v in (evidence or [])],
    )


def test_pipeline_correlated_pass(cli_env: object) -> None:
    """S01：collector publish 与 sink consume 共享 correlation_id 且证据关联 → PASS/0。"""
    server = cli_env.server

    async def diagnose(request: Any, context: Any) -> Any:
        if "publish" in request.stages:
            return common_pb2.DiagnoseResponse(
                overall_status="PASS",
                stages=[
                    _stage("sample", "PASS", [("sample_id", "s-1")]),
                    _stage("publish", "PASS", [("message_id", "m-shared")]),
                ],
            )
        return common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[
                _stage("consume", "PASS", [("message_id", "m-shared")]),
                _stage("dispatch", "PASS"),
            ],
        )

    server.respond(server.method_path("CommonService", "Diagnose"), diagnose)
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1")
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["overall_status"] in {"PASS", "SUCCESS"}


def test_pipeline_missing_correlation_fails(cli_env: object) -> None:
    """S03：两侧证据无法关联时 CLI 必须判失败（1），不得输出端到端 PASS。"""
    server = cli_env.server

    async def diagnose(request: Any, context: Any) -> Any:
        if "publish" in request.stages:
            return common_pb2.DiagnoseResponse(
                overall_status="PASS",
                stages=[_stage("publish", "PASS", [("message_id", "id-A")])],
            )
        return common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[_stage("consume", "PASS", [("message_id", "id-B")])],
        )

    server.respond(server.method_path("CommonService", "Diagnose"), diagnose)
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1")
    assert result.exit_code in {1, 3}, result.stdout
    assert result.json()["status"] in {"FAILURE", "UNKNOWN"}
