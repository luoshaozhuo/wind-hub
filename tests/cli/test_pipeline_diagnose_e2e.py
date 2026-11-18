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
                        "NATS",
                        "PASS",
                        [("sample", json.dumps({"device_id": "d1", "sample_id": "s-1"}))],
                    ),
                ],
            )
        return common_pb2.DiagnoseResponse(
            overall_status="PASS",
            stages=[
                _stage("source", "PASS"),
                _stage(
                    "delivery",
                    "PASS",
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
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1", "--sink-id", "ems_power")
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
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1", "--sink-id", "ems_power")
    assert result.exit_code == 3, result.stdout
    assert result.json()["data"]["overall_status"] == "UNKNOWN"


def _sample_evidence(payload: dict[str, Any] | None, *, raw: str | None = None) -> Any:
    value = raw if raw is not None else json.dumps(payload)
    return common_pb2.Evidence(name="sample", value=value)


def _pair_behavior(
    collector_status: str,
    collector_evidence: list[Any],
    sink_status: str,
    sink_evidence: list[Any],
) -> Any:
    async def diagnose(request: Any, context: Any) -> Any:
        if "NATS" in request.stages:
            return common_pb2.DiagnoseResponse(
                overall_status=collector_status,
                stages=[
                    common_pb2.DiagnosticStage(
                        stage="NATS", status="PASS", evidence=collector_evidence
                    )
                ],
            )
        return common_pb2.DiagnoseResponse(
            overall_status=sink_status,
            stages=[
                common_pb2.DiagnosticStage(stage="delivery", status="PASS", evidence=sink_evidence)
            ],
        )

    return diagnose


@pytest.mark.parametrize(
    ("collector_payload", "sink_payload", "case_id"),
    [
        # 同设备、不同 sample_id：无交集。
        (
            {"device_id": "d1", "sample_id": "s-a"},
            {"device_id": "d1", "sample_id": "s-b"},
            "same-device-different-id",
        ),
        # 缺少 device_id（任一端）。
        (
            {"sample_id": "s-1"},
            {"device_id": "d1", "sample_id": "s-1"},
            "collector-missing-device-id",
        ),
        (
            {"device_id": "d1", "sample_id": "s-1"},
            {"sample_id": "s-1"},
            "sink-missing-device-id",
        ),
        # 空 sample_id。
        (
            {"device_id": "d1", "sample_id": ""},
            {"device_id": "d1", "sample_id": ""},
            "empty-sample-id",
        ),
        # sample_id 错误类型（数字/对象）。
        (
            {"device_id": "d1", "sample_id": 123},
            {"device_id": "d1", "sample_id": 123},
            "numeric-sample-id",
        ),
        (
            {"device_id": "d1", "sample_id": {"nested": True}},
            {"device_id": "d1", "sample_id": {"nested": True}},
            "object-sample-id",
        ),
        # 一端结构化、一端仅匿名 sample_id 证据名。
        (
            {"device_id": "d1", "sample_id": "s-1"},
            None,
            "sink-anonymous-sample-id",
        ),
        # 同 ID 但带额外字段不得影响判定，这里给错设备验证严格匹配。
        (
            {"device_id": "d1", "sample_id": "s-1", "point_id": "wind_speed"},
            {"device_id": "other-device", "sample_id": "s-1", "point_id": "wind_speed"},
            "extra-fields-wrong-device",
        ),
    ],
)
def test_pipeline_structured_evidence_matrix_rejects_unprovable_pairs(
    cli_env: object,
    collector_payload: dict[str, Any] | None,
    sink_payload: dict[str, Any] | None,
    case_id: str,
) -> None:
    server = cli_env.server
    collector_evidence = (
        [_sample_evidence(collector_payload)] if collector_payload is not None else []
    )
    sink_evidence = [_sample_evidence(sink_payload)] if sink_payload is not None else []
    if case_id == "sink-anonymous-sample-id":
        sink_evidence = [common_pb2.Evidence(name="sample_id", value="s-1")]
    server.respond(
        server.method_path("CommonService", "Diagnose"),
        _pair_behavior("PASS", collector_evidence, "SUCCESS", sink_evidence),
    )
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1", "--sink-id", "ems_power")
    assert result.exit_code == 3, (case_id, result.stdout)
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN", (case_id, envelope)
    assert envelope["data"]["overall_status"] == "UNKNOWN", (case_id, envelope)


def test_pipeline_structured_matrix_accepts_exact_device_bound_match(
    cli_env: object,
) -> None:
    """同设备同 sample_id 且双方成功：唯一允许的 PASS 路径。"""
    server = cli_env.server
    payload = {"device_id": "d1", "point_id": "wind_speed", "sample_id": "s-1"}
    server.respond(
        server.method_path("CommonService", "Diagnose"),
        _pair_behavior(
            "PASS",
            [_sample_evidence(payload)],
            "SUCCESS",
            [_sample_evidence(payload)],
        ),
    )
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1", "--sink-id", "ems_power")
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["overall_status"] == "PASS"


def test_pipeline_failed_service_maps_to_exit_one_even_with_matching_ids(
    cli_env: object,
) -> None:
    """任一服务 FAILED 时，即使样本 ID 相交也必须判 FAIL/exit 1。"""
    server = cli_env.server
    payload = {"device_id": "d1", "sample_id": "s-1"}
    server.respond(
        server.method_path("CommonService", "Diagnose"),
        _pair_behavior(
            "PASS",
            [_sample_evidence(payload)],
            "FAILED",
            [_sample_evidence(payload)],
        ),
    )
    result = cli_env.subprocess_invoke("diagnose", "pipeline", "d1", "--sink-id", "ems_power")
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] == "FAILURE"
    assert result.json()["data"]["overall_status"] == "FAIL"
