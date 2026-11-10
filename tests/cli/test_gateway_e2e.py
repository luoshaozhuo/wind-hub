"""UC-10 Industrial Gateway System Test（真实 CLI 子进程 + 真实 mTLS/gRPC 链路）。

覆盖 ListVariables→UpdateVariables→ReadVariables 闭环与表 4.x 规定的：
部分成功、全失败、结果数不符、变量 ID 错序、非法标量、重复变量、确认门、
服务端 UNIMPLEMENTED 能力门。服务端为 grpc.aio 真服务器，业务行为注入；
生产 Gateway 服务端实现验收不在本文件范围（NOT_RUN 项见验收报告）。
"""

from __future__ import annotations

from typing import Any

import grpc
import pytest

from contracts import industrial_gateway_pb2 as gw_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _path(server: Any, method: str) -> str:
    return server.method_path("IndustrialGatewayService", method)


def _install_lease(server: Any, *, token: str = "lease-token-1") -> None:
    """Fake the real Gateway lease RPC: ACQUIRE issues a token, RELEASE confirms."""

    async def on_claim(request: Any, context: Any) -> Any:
        if request.action == "ACQUIRE":
            return gw_pb2.ClaimTargetsResponse(accepted=True, lease_token=token)
        if request.action == "RELEASE" and request.lease_token == token:
            return gw_pb2.ClaimTargetsResponse(accepted=True)
        return gw_pb2.ClaimTargetsResponse(accepted=False, error_code="INVALID_CLAIM")

    server.respond(_path(server, "ClaimTargets"), on_claim)


def _claim_paths(server: Any) -> list[str]:
    return [c.path for c in server.calls if c.path.endswith("/ClaimTargets")]


def test_gateway_closed_loop_list_update_read(cli_env: Any) -> None:
    """UC-10-S01：列表→读→批量更新（部分成功）→再读，真实进程真实链路。"""
    server = cli_env.server
    _install_lease(server)
    server.respond_value(
        _path(server, "ListVariables"),
        gw_pb2.ListVariablesResponse(
            variables=[
                gw_pb2.Variable(variable_id="v1", name="转速", data_type="SINT64", writable=True),
                gw_pb2.Variable(variable_id="v2", name="电压", data_type="DOUBLE", writable=True),
            ]
        ),
    )

    captured: dict[str, Any] = {}

    async def on_update(request: Any, context: Any) -> Any:
        captured["request"] = request
        return gw_pb2.UpdateVariablesResponse(
            results=[
                gw_pb2.VariableUpdateResult(variable_id="v1", success=True),
                gw_pb2.VariableUpdateResult(
                    variable_id="v2", success=False, error_code="OUT_OF_RANGE", message="x"
                ),
            ]
        )

    server.respond(_path(server, "UpdateVariables"), on_update)
    server.respond_value(
        _path(server, "ReadVariables"),
        gw_pb2.ReadVariablesResponse(gateway_id="gw1"),
    )

    listing = cli_env.subprocess_invoke("gateway", "variables", "gw1")
    assert listing.exit_code == 0, listing.stdout
    assert len(listing.json()["data"]["variables"]) == 2

    update = cli_env.subprocess_invoke(
        "gateway", "update", "gw1", "v1=123", "v2=1.5", "--owner-id", "tester", "--yes"
    )
    # 部分成功 → PARTIAL/4（不阻断其他独立服务的设计在 service status 用例验证）
    assert update.exit_code == 4, update.stdout
    envelope = update.json()
    assert envelope["status"] == "PARTIAL"
    request = captured["request"]
    assert request.gateway_id == "gw1"
    assert request.lease_token == "lease-token-1"
    assert request.values[0].value.sint64_value == 123
    assert request.values[0].value.HasField("sint64_value")
    assert request.values[1].value.double_value == pytest.approx(1.5)
    # 租约完整流转：先 ACQUIRE 后 RELEASE，更新只发一次。
    claim_requests = [call.request for call in server.calls if call.path.endswith("/ClaimTargets")]
    assert [item.action for item in claim_requests] == ["ACQUIRE", "RELEASE"]
    assert claim_requests[0].owner_id == "tester"
    assert claim_requests[0].variable_ids == ["v1", "v2"]
    assert claim_requests[1].lease_token == "lease-token-1"

    read = cli_env.subprocess_invoke("gateway", "read", "gw1", "v1", "v2")
    assert read.exit_code == 0, read.stdout
    assert read.json()["data"]["gateway_id"] == "gw1"


def test_gateway_update_requires_confirmation(cli_env: Any) -> None:
    """确认门：非交互且无 --yes → ABORTED/2，且不得发出 RPC（含租约申请）。"""
    result = cli_env.subprocess_invoke("gateway", "update", "gw1", "v1=1", "--owner-id", "tester")
    assert result.exit_code == 2, result.stdout
    assert result.json()["status"] in {"ABORTED", "FAILURE"}
    assert not cli_env.server.calls


def test_lease_ms_range_and_token(cli_env: Any) -> None:
    """--lease-ms 限定 1000..60000 ms：越界在命令边界本地拒绝，不发任何 RPC。"""
    for bad_ms in (999, 60_001):
        cli_env.server.calls.clear()
        result = cli_env.subprocess_invoke(
            "gateway",
            "update",
            "gw1",
            "v1=1",
            "--owner-id",
            "tester",
            "--lease-ms",
            str(bad_ms),
            "--yes",
        )
        assert result.exit_code == 2, result.stdout
        assert not cli_env.server.calls  # 无效租约不得调用 ClaimTargets/UpdateVariables


def test_gateway_update_accepts_lease_ms_bounds_and_passes_value(cli_env: Any) -> None:
    """边界值 1000/60000 可受理，且 lease_ms 原样进入 ClaimTargets 请求。"""
    server = cli_env.server
    captured: dict[str, Any] = {}

    async def on_claim(request: Any, context: Any) -> Any:
        if request.action == "ACQUIRE":
            captured["lease_ms"] = request.lease_ms
            return gw_pb2.ClaimTargetsResponse(accepted=True, lease_token="lease-token-1")
        return gw_pb2.ClaimTargetsResponse(accepted=True)

    server.respond(_path(server, "ClaimTargets"), on_claim)
    server.respond_value(
        _path(server, "UpdateVariables"),
        gw_pb2.UpdateVariablesResponse(
            results=[gw_pb2.VariableUpdateResult(variable_id="v1", success=True)]
        ),
    )
    for boundary_ms in (1000, 60_000):
        captured.clear()
        result = cli_env.subprocess_invoke(
            "gateway",
            "update",
            "gw1",
            "v1=1",
            "--owner-id",
            "tester",
            "--lease-ms",
            str(boundary_ms),
            "--yes",
        )
        assert result.exit_code == 0, result.stdout
        assert captured["lease_ms"] == boundary_ms


def test_gateway_update_all_fail_exit_1(cli_env: Any) -> None:
    _install_lease(cli_env.server)
    cli_env.server.respond_value(
        _path(cli_env.server, "UpdateVariables"),
        gw_pb2.UpdateVariablesResponse(
            results=[
                gw_pb2.VariableUpdateResult(variable_id="v1", success=False, error_code="REJECTED")
            ]
        ),
    )
    result = cli_env.subprocess_invoke(
        "gateway", "update", "gw1", "v1=1", "--owner-id", "tester", "--yes"
    )
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] == "FAILURE"


def test_gateway_update_denied_lease_never_dispatches_update(cli_env: Any) -> None:
    """租约申请被拒：按业务参数错误退出，且不得发出 UpdateVariables。"""
    server = cli_env.server

    async def deny(request: Any, context: Any) -> Any:
        return gw_pb2.ClaimTargetsResponse(accepted=False, error_code="TARGET_CLAIMED")

    server.respond(_path(server, "ClaimTargets"), deny)
    result = cli_env.subprocess_invoke(
        "gateway", "update", "gw1", "v1=1", "--owner-id", "tester", "--yes"
    )
    assert result.exit_code == 2, result.stdout
    assert "TARGET_CLAIMED" in result.stdout
    assert _update_paths(server) == []
    assert _claim_paths(server) == [_path(server, "ClaimTargets")]


def _update_paths(server: Any) -> list[str]:
    return [c.path for c in server.calls if c.path.endswith("/UpdateVariables")]


def test_gateway_update_result_count_mismatch_is_unknown_no_retry(cli_env: Any) -> None:
    """服务端结果数少于请求数：写入结果不明 → UNKNOWN/3，绝不重发、绝不伪称成功。"""
    server = cli_env.server
    _install_lease(server)
    server.respond_value(
        _path(server, "UpdateVariables"),
        gw_pb2.UpdateVariablesResponse(
            results=[gw_pb2.VariableUpdateResult(variable_id="v1", success=True)]
        ),
    )
    result = cli_env.subprocess_invoke(
        "gateway", "update", "gw1", "v1=1", "v2=2", "--owner-id", "tester", "--yes"
    )
    # 数量不一致无法判断各点写入结果，等同结果不明（UNKNOWN/3），不是业务失败（1）。
    assert result.exit_code == 3, result.stdout
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN"
    message = envelope["message"].lower()
    assert "unknown" in message and "do not retry" in message
    # 真实线上调用计数：只发出过一次 UpdateVariables，无任何自动重试。
    assert _update_paths(server) == [_path(server, "UpdateVariables")]
    # 失败路径也必须释放租约，不得遗留占用。
    assert [c.request.action for c in server.calls if c.path.endswith("/ClaimTargets")] == [
        "ACQUIRE",
        "RELEASE",
    ]


def test_gateway_update_result_id_order_mismatch_is_unknown_no_retry(cli_env: Any) -> None:
    """结果 ID 集合/顺序与请求不符：UNKNOWN/3，仅发一次，不重发。"""
    server = cli_env.server
    _install_lease(server)
    server.respond_value(
        _path(server, "UpdateVariables"),
        gw_pb2.UpdateVariablesResponse(
            results=[
                gw_pb2.VariableUpdateResult(variable_id="v2", success=True),
                gw_pb2.VariableUpdateResult(variable_id="v1", success=True),
            ]
        ),
    )
    result = cli_env.subprocess_invoke(
        "gateway", "update", "gw1", "v1=1", "v2=2", "--owner-id", "tester", "--yes"
    )
    assert result.exit_code == 3, result.stdout
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN"
    message = envelope["message"].lower()
    assert "unknown" in message and "do not retry" in message
    assert _update_paths(server) == [_path(server, "UpdateVariables")]


def test_gateway_update_illegal_scalar_exit_2(cli_env: Any) -> None:
    # 数组不是标量；NaN 不是有限 JSON；两者都不得发出 RPC。
    for bad in ("v1=[1,2]", "v1=NaN"):
        result = cli_env.subprocess_invoke(
            "gateway", "update", "gw1", bad, "--owner-id", "tester", "--yes"
        )
        assert result.exit_code == 2, (bad, result.stdout)
    assert not cli_env.server.calls


def test_gateway_update_duplicate_variable_rejected(cli_env: Any) -> None:
    result = cli_env.subprocess_invoke(
        "gateway", "update", "gw1", "v1=1", "v1=2", "--owner-id", "tester", "--yes"
    )
    assert result.exit_code == 2, result.stdout
    assert not cli_env.server.calls


def test_gateway_unimplemented_capability_exit_3(cli_env: Any) -> None:
    """服务端未实现能力（UNIMPLEMENTED）必须如实报 3，不得在客户端冒充成功。"""
    cli_env.server.fail(
        _path(cli_env.server, "ListGateways"), grpc.StatusCode.UNIMPLEMENTED, "no gateway"
    )
    result = cli_env.subprocess_invoke("gateway", "list")
    assert result.exit_code == 3, result.stdout
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN"
    assert envelope["error_code"] == "UNIMPLEMENTED"


def test_gateway_unreachable_exit_3(cli_env: Any, tmp_path: Any) -> None:
    """UC-10-S03：端点不可达 → 3，且无异常堆栈泄漏。"""
    from tests.cli.conftest import _write_certs
    from tests.cli.e2e_util import last_json, run_cli, write_cli_yaml

    certs = _write_certs(tmp_path / "certs")
    cfg = write_cli_yaml(
        tmp_path / "cli.yaml",
        certs,
        {"gateway": "localhost:1"},
        timeout=1.0,
    )
    completed = run_cli(cfg, "gateway", "list", timeout=20)
    assert completed.returncode == 3, completed.stdout
    envelope = last_json(completed.stdout)
    assert envelope["error_code"] in {"UNAVAILABLE", "DEADLINE_EXCEEDED"}
    assert "Traceback" not in completed.stderr


def test_gateway_update_deadline_unknown_single_dispatch(cli_env: Any, tmp_path: Any) -> None:
    """在途 UpdateVariables 超过本地总预算：UNKNOWN/3，线上仅一次调用，绝不重发。"""
    import asyncio

    from tests.cli.e2e_util import last_json, run_cli, write_cli_yaml

    async def never_responds(request: Any, context: Any) -> Any:
        await asyncio.sleep(30)
        return gw_pb2.UpdateVariablesResponse()

    server = cli_env.server
    _install_lease(server)
    server.respond(_path(server, "UpdateVariables"), never_responds)
    cfg = write_cli_yaml(
        tmp_path / "cli.yaml",
        server.cert_files,
        {"gateway": f"localhost:{server.port}"},
        timeout=1.0,
    )
    completed = run_cli(
        cfg,
        "gateway",
        "update",
        "gw1",
        "v1=1",
        "--owner-id",
        "tester",
        "--yes",
        timeout=20,
    )
    assert completed.returncode == 3, completed.stdout
    envelope = last_json(completed.stdout)
    assert envelope["status"] == "UNKNOWN"
    # 服务端真实记账：只收到过一次 UpdateVariables（超时未触发任何重试）。
    assert _update_paths(server) == [_path(server, "UpdateVariables")]


def test_gateway_start_stop_revision_prefetch_and_confirm(cli_env: Any) -> None:
    """UC-10-S01：网关实例启停先取 revision 再下发 OperationRequest，确认门不发 RPC。"""
    server = cli_env.server
    server.respond_value(
        _path(server, "GetGateway"),
        gw_pb2.GetGatewayResponse(
            gateway=gw_pb2.Gateway(
                gateway_id="gw1", desired_state="STOPPED", actual_state="STOPPED", revision="rv1"
            )
        ),
    )
    from contracts import common_pb2

    async def start_or_stop(request: Any, context: Any) -> Any:
        return common_pb2.OperationResult(
            operation_id="op1",
            accepted=True,
            desired_state="STARTED",
            actual_state="STARTED",
            revision="rv2",
        )

    server.respond(_path(server, "StartGateway"), start_or_stop)
    server.respond(_path(server, "StopGateway"), start_or_stop)

    blocked = cli_env.subprocess_invoke("gateway", "start", "gw1")
    assert blocked.exit_code == 2, blocked.stdout
    assert not [c for c in server.calls if c.path.endswith("/StartGateway")]

    started = cli_env.subprocess_invoke("gateway", "start", "gw1", "--yes")
    assert started.exit_code == 0, started.stdout
    call = [c for c in server.calls if c.path.endswith("/StartGateway")][0]
    assert call.request.id == "gw1"
    assert call.request.expected_revision == "rv1"

    stopped = cli_env.subprocess_invoke(
        "gateway", "stop", "gw1", "--expected-revision", "rv9", "--yes"
    )
    assert stopped.exit_code == 0, stopped.stdout
    stop_call = [c for c in server.calls if c.path.endswith("/StopGateway")][0]
    assert stop_call.request.expected_revision == "rv9"
