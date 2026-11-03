"""跨服务诊断关联门与双 Sink 进程身份（UC-14/UC-07）。"""

from __future__ import annotations

from typing import Any

import pytest

from cli.runtime import Runtime
from contracts import common_pb2

pytestmark = pytest.mark.contract

_PATH = "/windhub.common.v1.CommonService/Diagnose"


def _diagnose_response(status: str, ids: list[str]) -> common_pb2.DiagnoseResponse:
    return common_pb2.DiagnoseResponse(
        overall_status=status,
        stages=[
            common_pb2.DiagnosticStage(
                stage="publish",
                status="PASS",
                evidence=[common_pb2.Evidence(name="message_id", value=value) for value in ids],
            )
        ],
    )


def test_diagnose_pipeline_requires_correlated_evidence(cli_env: Any) -> None:
    server = cli_env.server

    async def behavior(request: Any, context: Any) -> Any:
        # 两侧必须共享同一 correlation_id；消息级证据无交集时不得判 PASS。
        if list(request.stages) == ["connect", "protocol", "sample", "publish"]:
            return _diagnose_response("PASS", ["m-1", "m-2"])
        return _diagnose_response("PASS", ["m-9"])

    server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "dev-1")
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
        if "consume" in request.stages:
            return _diagnose_response("PASS", ["m-2"])
        return _diagnose_response("PASS", ["m-1", "m-2"])

    cli_env.server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "dev-1")
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["overall_status"] == "PASS"


def test_diagnose_pipeline_failure_is_exit1(cli_env: Any) -> None:
    async def behavior(request: Any, context: Any) -> Any:
        if "consume" in request.stages:
            return _diagnose_response("FAIL", [])
        return _diagnose_response("PASS", ["m-2"])

    cli_env.server.respond(_PATH, behavior)
    result = cli_env.invoke("diagnose", "pipeline", "dev-1")
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] == "FAILURE"


def test_dual_sink_identities_remain_distinct(cli_env: Any) -> None:
    runtime = Runtime(cli_env.config_path, "json")
    try:
        assert runtime.target("sink").service_id == "sink_realtime"
        assert runtime.target("sink_realtime").service_id == "sink_realtime"
        assert runtime.target("sink_reliable").service_id == "sink_reliable"
    finally:
        import asyncio

        asyncio.run(runtime.close())
