"""UC-13 跨服务数据链路诊断 System Test：无关联证据不得宣称端到端 PASS。"""

from __future__ import annotations

import json
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
        if "NATS" in request.stages:
            return common_pb2.DiagnoseResponse(
                overall_status="PASS",
                stages=[
                    _stage(
                        "NATS", "PASS",
                        [("sample", json.dumps({"device_id": "d1", "sample_id": "s-1"}))],
                    ),
                ],
            )
        return common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[
                _stage("source", "PASS"),
                _stage(
                    "delivery", "PASS",
                    [("sample", json.dumps({"device_id": "d1", "sample_id": "s-1"}))],
                ),
            ],
        )

    server.respond(server.method_path("CommonService", "Diagnose"), diagnose)
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1", "--sink-id", "ems_power")
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["overall_status"] in {"PASS", "SUCCESS"}


def test_pipeline_missing_correlation_fails(cli_env: object) -> None:
    """S03：两侧证据无法关联时 CLI 必须判失败（1），不得输出端到端 PASS。"""
    server = cli_env.server

    async def diagnose(request: Any, context: Any) -> Any:
        if "NATS" in request.stages:
            return common_pb2.DiagnoseResponse(
                overall_status="PASS",
                stages=[_stage("publish", "PASS", [("message_id", "id-A")])],
            )
        return common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[_stage("consume", "PASS", [("message_id", "id-B")])],
        )

    server.respond(server.method_path("CommonService", "Diagnose"), diagnose)
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1", "--sink-id", "ems_power")
    assert result.exit_code in {1, 3}, result.stdout
    assert result.json()["status"] in {"FAILURE", "UNKNOWN"}


def test_pipeline_other_device_traffic_cannot_pass_target(cli_env: object) -> None:
    server = cli_env.server

    def evidence(device: str, sample_id: str) -> Any:
        return common_pb2.Evidence(
            name="sample",
            value=json.dumps(
                {
                    "device_id": device,
                    "point_id": "wind_speed",
                    "sample_id": sample_id,
                    "confirmed_at": "2026-01-01T00:00:00Z",
                }
            ),
        )

    async def diagnose(request: Any, context: Any) -> Any:
        if "NATS" in request.stages:
            return common_pb2.DiagnoseResponse(
                overall_status="PASS",
                stages=[
                    _stage("NATS", "PASS"),
                    common_pb2.DiagnosticStage(
                        stage="NATS",
                        status="PASS",
                        evidence=[evidence("A", "a-1"), evidence("B", "b-1")],
                    ),
                ],
            )
        return common_pb2.DiagnoseResponse(
            overall_status="SUCCESS",
            stages=[
                _stage("source", "SUCCESS"),
                common_pb2.DiagnosticStage(
                    stage="delivery", status="SUCCESS", evidence=[evidence("A", "a-1")]
                ),
            ],
        )

    server.respond(server.method_path("CommonService", "Diagnose"), diagnose)
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "B", "--sink-id", "ems_power")
    assert result.exit_code == 3, result.stdout
    assert result.json()["data"]["overall_status"] == "UNKNOWN"


def test_pipeline_rejects_matching_anonymous_ids(cli_env: object) -> None:
    """Matching legacy message IDs must not prove any device-specific delivery."""
    server = cli_env.server

    async def diagnose(request: Any, context: Any) -> Any:
        return common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[_stage("NATS", "PASS", [("message_id", "shared-id")])],
        )

    server.respond(server.method_path("CommonService", "Diagnose"), diagnose)
    result = cli_env.subprocess_invoke(
        "diagnose", "pipeline", "d1", "--sink-id", "ems_power"
    )
    assert result.exit_code == 3, result.stdout
    assert result.json()["data"]["overall_status"] == "UNKNOWN"


def test_pipeline_rejects_malformed_structured_evidence(cli_env: object) -> None:
    """Malformed remote evidence must not cause false PASS or an uncaught error."""
    server = cli_env.server

    async def diagnose(request: Any, context: Any) -> Any:
        return common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[_stage("NATS", "PASS", [("sample", "{invalid-json")])],
        )

    server.respond(server.method_path("CommonService", "Diagnose"), diagnose)
    result = cli_env.subprocess_invoke(
        "diagnose", "pipeline", "d1", "--sink-id", "ems_power"
    )
    assert result.exit_code == 3, result.stdout
    assert result.json()["data"]["overall_status"] == "UNKNOWN"
