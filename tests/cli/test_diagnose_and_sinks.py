"""跨服务诊断关联与单 Main Sink 管理身份（UC-14/UC-07）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cli.runtime import Runtime
from contracts import common_pb2

pytestmark = pytest.mark.contract

_PATH = "/windhub.common.v1.CommonService/Diagnose"


def _diagnose_response(
    status: str, ids: list[str], *, device_id: str = "dev-1"
) -> common_pb2.DiagnoseResponse:
    return common_pb2.DiagnoseResponse(
        overall_status=status,
        stages=[
            common_pb2.DiagnosticStage(
                stage="publish",
                status="PASS",
                evidence=[
                    common_pb2.Evidence(
                        name="sample",
                        value=json.dumps({"device_id": device_id, "sample_id": value}),
                    )
                    for value in ids
                ],
            )
        ],
    )


def test_diagnose_pipeline_requires_correlated_evidence(cli_env: Any) -> None:
    server = cli_env.server

    async def behavior(request: Any, context: Any) -> Any:
        # 两侧必须共享同一 correlation_id；消息级证据无交集时不得判 PASS。
        if "NATS" in request.stages:
            return _diagnose_response("PASS", ["m-1", "m-2"])
        return _diagnose_response("PASS", ["m-9"])

    server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "dev-1", "--sink-id", "delivery-dev1")
    assert result.exit_code == 3, result.stdout
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN"
    assert "message/sample ID" in envelope["data"]["limitations"][0]
    diagnose_calls = [call for call in server.calls if call.path.endswith("/Diagnose")]
    assert len(diagnose_calls) == 2
    correlation_ids = {call.request.correlation_id for call in diagnose_calls}
    assert len(correlation_ids) == 1


def test_diagnose_pipeline_pass_with_shared_id(cli_env: Any) -> None:
    async def behavior(request: Any, context: Any) -> Any:
        if "consumer" in request.stages:
            return _diagnose_response("PASS", ["m-2"])
        return _diagnose_response("PASS", ["m-1", "m-2"])

    cli_env.server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "dev-1", "--sink-id", "delivery-dev1")
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["overall_status"] == "PASS"


def test_diagnose_pipeline_failure_is_exit1(cli_env: Any) -> None:
    async def behavior(request: Any, context: Any) -> Any:
        if "consumer" in request.stages:
            return _diagnose_response("FAIL", [])
        return _diagnose_response("PASS", ["m-2"])

    cli_env.server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "dev-1", "--sink-id", "delivery-dev1")
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] == "FAILURE"


def test_diagnose_output_is_private_and_never_overwritten(cli_env: Any, tmp_path: Path) -> None:
    cli_env.server.respond_value(_PATH, _diagnose_response("PASS", []))
    output = tmp_path / "diagnose.json"

    first = cli_env.invoke("diagnose", "commander", "dev-1", "--output", str(output))

    assert first.exit_code == 0, first.stdout
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["overall_status"] == "PASS"
    original = output.read_bytes()

    second = cli_env.invoke("diagnose", "commander", "dev-1", "--output", str(output))

    assert second.exit_code == 2
    assert second.json()["error_code"] == "FileExistsError"
    assert output.read_bytes() == original


def test_diagnose_output_exception_uses_json_envelope(cli_env: Any, tmp_path: Path) -> None:
    cli_env.server.respond_value(_PATH, _diagnose_response("PASS", []))

    result = cli_env.invoke(
        "diagnose",
        "commander",
        "dev-1",
        "--output",
        str(tmp_path / "missing" / "diagnose.json"),
    )

    assert result.exit_code == 2
    assert result.json()["error_code"] == "ValueError"


def test_sink_uses_single_service_identity(cli_env: Any) -> None:
    runtime = Runtime(cli_env.config_path, "json")
    try:
        assert runtime.target("sink").service_id == "sink"
        with pytest.raises(ValueError, match="not configured"):
            runtime.target("sink_realtime")
        with pytest.raises(ValueError, match="not configured"):
            runtime.target("sink_reliable")
    finally:
        import asyncio

        asyncio.run(runtime.close())


def test_diagnose_pipeline_requires_explicit_sink_id(cli_env: Any) -> None:
    # G-04：缺少 --sink-id 必须在本地拒绝，禁止把 device_id 当 Delivery ID。
    result = cli_env.invoke("diagnose", "pipeline", "dev-1")
    assert result.exit_code == 2, result.stdout
    assert not [call for call in cli_env.server.calls if call.path.endswith("/Diagnose")]


def test_diagnose_pipeline_routes_explicit_delivery_id_to_sink(cli_env: Any) -> None:
    server = cli_env.server
    targets: list[tuple[str, str]] = []

    async def behavior(request: Any, context: Any) -> Any:
        targets.append((tuple(request.stages)[0], request.target_id))
        return _diagnose_response("PASS", ["m-1"], device_id="WTG_SIM01")

    server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "WTG_SIM01", "--sink-id", "ems_power")
    assert result.exit_code == 0, result.stdout
    assert ("NETWORK", "WTG_SIM01") in targets
    # Sink 侧必须收到显式 Delivery ID，而不是设备 ID。
    assert ("source", "ems_power") in targets
    assert all(target != ("source", "WTG_SIM01") for _, target in targets)


def test_diagnose_pipeline_unknown_delivery_is_business_failure(cli_env: Any) -> None:
    import grpc

    server = cli_env.server

    async def behavior(request: Any, context: Any) -> Any:
        if "NATS" in request.stages:
            return _diagnose_response("PASS", ["m-1"])
        await context.abort(grpc.StatusCode.NOT_FOUND, "unknown Delivery")

    server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "dev-1", "--sink-id", "missing-delivery")
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] == "FAILURE"
    assert len([call for call in server.calls if call.path.endswith("/Diagnose")]) == 2
