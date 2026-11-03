"""UC-03 服务身份、健康与能力 System Test；含 service status 部分降级独立性。"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def test_service_info_and_capabilities_roundtrip(cli_env: Any) -> None:
    """S01/S02：GetServiceInfo + GetCapabilities 五服务统一通道。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetServiceInfo"),
        common_pb2.GetServiceInfoResponse(
            service_id="collector", software_version="1.2.3", contract_version="v1"
        ),
    )
    server.respond_value(
        server.method_path("CommonService", "GetCapabilities"),
        common_pb2.GetCapabilitiesResponse(
            rpc_names=["ListDevices", "ReadPoints"], protocols=["modbus"]
        ),
    )
    info = cli_env.subprocess_invoke("service", "info", "collector")
    assert info.exit_code == 0, info.stdout
    assert info.json()["data"]["software_version"] == "1.2.3"

    caps = cli_env.subprocess_invoke("service", "capabilities", "collector")
    assert caps.exit_code == 0, caps.stdout
    assert "ReadPoints" in caps.json()["data"]["rpc_names"]


def test_capability_unimplemented_exit3(cli_env: Any) -> None:
    """S03：能力 RPC 未实现必须如实报 3，不得客户端伪装。"""
    cli_env.server.fail(
        cli_env.server.method_path("CommonService", "GetCapabilities"),
        grpc.StatusCode.UNIMPLEMENTED,
        "no caps",
    )
    result = cli_env.subprocess_invoke("service", "capabilities", "gateway")
    assert result.exit_code == 3, result.stdout
    assert result.json()["status"] == "UNKNOWN"


def test_service_status_partial_degradation_is_independent(cli_env: Any, tmp_path: Any) -> None:
    """S03：collector 健康、其余端点不可达 → PARTIAL/4，健康服务结果不被阻断。"""
    from tests.cli.e2e_util import last_json, run_cli, write_cli_yaml

    good = f"localhost:{cli_env.server.port}"
    cli_env.server.respond_value(
        cli_env.server.method_path("CommonService", "GetServiceStatus"),
        common_pb2.GetServiceStatusResponse(health="HEALTHY"),
    )
    cfg = write_cli_yaml(
        tmp_path / "cli.yaml",
        cli_env.server.cert_files,
        {
            "collector": good,
            "commander": "localhost:1",
            "sink_realtime": "localhost:1",
            "sink_reliable": "localhost:1",
            "gateway": "localhost:1",
            "data_generator": "localhost:1",
        },
        timeout=1.0,
    )
    completed = run_cli(cfg, "service", "status", timeout=40)
    assert completed.returncode == 4, completed.stdout
    envelope = last_json(completed.stdout)
    assert envelope["status"] == "PARTIAL"
    services = {item["service"]: item for item in envelope["data"]}
    assert services["collector"]["business"]["health"] == "HEALTHY"
    assert services["commander"]["business"]["health"] == "UNKNOWN"


def test_service_status_single_name(cli_env: Any) -> None:
    """按名查询：只请求该服务，全 HEALTHY → 0。"""
    cli_env.server.respond_value(
        cli_env.server.method_path("CommonService", "GetServiceStatus"),
        common_pb2.GetServiceStatusResponse(health="READY"),
    )
    result = cli_env.subprocess_invoke("service", "status", "collector")
    assert result.exit_code == 0, result.stdout
    assert len(result.json()["data"]) == 1
