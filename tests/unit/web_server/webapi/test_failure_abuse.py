"""Admin API v1 失败/滥用矩阵（进程内，无真实网络）。

覆盖：Operation 取消的 404/409/200、command_id 冲突 409、gRPC 故障到
HTTP 的稳定映射（UNAVAILABLE→503、NOT_FOUND→404）、诊断输入校验 422，
以及重复请求不造成重复执行的控制安全语义。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import grpc
from fastapi.testclient import TestClient

from web_server.adapter.inbound.webapi.app import build_api
from web_server.application.app_context import AppContext
from web_server.application.device.command import CommandIdConflictError
from web_server.application.operation.registry import OperationRegistry


def _rpc_error(code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, None, None, details=f"simulated {code.name}")


def _client(ctx: AppContext) -> TestClient:
    return TestClient(build_api(ctx))


# ---------------------------------------------------------------------------
# Operation 取消
# ---------------------------------------------------------------------------


def test_cancel_unknown_operation_returns_404() -> None:
    diagnostics = MagicMock()
    diagnostics.cancel_operation.side_effect = KeyError("ghost")
    client = _client(AppContext(diagnostics=diagnostics))

    response = client.post("/api/v1/operations/ghost/cancel")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_cancel_terminal_operation_returns_409() -> None:
    diagnostics = MagicMock()
    diagnostics.cancel_operation.side_effect = ValueError(
        "operation 'op-1' is already terminal"
    )
    client = _client(AppContext(diagnostics=diagnostics))

    response = client.post("/api/v1/operations/op-1/cancel")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OPERATION_NOT_CANCELLABLE"


def test_cancel_running_operation_returns_snapshot() -> None:
    operations = OperationRegistry()
    record = operations.create("diagnostics.subnet_scan")
    diagnostics = MagicMock()
    diagnostics.cancel_operation.return_value = operations.cancel(record.operation_id)
    client = _client(AppContext(diagnostics=diagnostics))

    response = client.post(f"/api/v1/operations/{record.operation_id}/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"


# ---------------------------------------------------------------------------
# 控制命令幂等冲突
# ---------------------------------------------------------------------------


def test_command_id_conflict_maps_to_409() -> None:
    control = MagicMock()
    control.send = AsyncMock(
        side_effect=CommandIdConflictError("command_id 'c1' was used with different content")
    )
    client = _client(AppContext(device_control=control))

    response = client.post(
        "/api/v1/devices/d1/commands",
        json={"point_id": "p1", "value": 2.0, "command_id": "c1"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMAND_ID_CONFLICT"


# ---------------------------------------------------------------------------
# gRPC 故障映射
# ---------------------------------------------------------------------------


def test_worker_unavailable_maps_to_503() -> None:
    diagnostics = MagicMock()
    diagnostics.read = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.UNAVAILABLE))
    client = _client(AppContext(diagnostics=diagnostics))

    response = client.post(
        "/api/v1/diagnostics/protocol/read",
        json={"device_id": "d1", "point_id": "p1"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WORKER_UNAVAILABLE"


def test_worker_not_found_maps_to_404() -> None:
    """未知设备/点由 Commander 返回 NOT_FOUND，不再被裸 except 吞成 503。"""
    diagnostics = MagicMock()
    diagnostics.read = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.NOT_FOUND))
    client = _client(AppContext(diagnostics=diagnostics))

    response = client.post(
        "/api/v1/diagnostics/protocol/read",
        json={"device_id": "ghost", "point_id": "p1"},
    )

    assert response.status_code == 404


def test_worker_deadline_maps_to_504() -> None:
    """超时不等于执行失败：独立映射为 504，调用方自行人工核对。"""
    diagnostics = MagicMock()
    diagnostics.read = AsyncMock(side_effect=_rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED))
    client = _client(AppContext(diagnostics=diagnostics))

    response = client.post(
        "/api/v1/diagnostics/protocol/read",
        json={"device_id": "d1", "point_id": "p1"},
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "WORKER_TIMEOUT"


# ---------------------------------------------------------------------------
# 输入边界
# ---------------------------------------------------------------------------


def test_subnet_scan_invalid_network_maps_to_422() -> None:
    diagnostics = MagicMock()
    diagnostics.start_subnet_scan.side_effect = ValueError("invalid network '999.0.0.0/8'")
    client = _client(AppContext(diagnostics=diagnostics))

    response = client.post(
        "/api/v1/diagnostics/subnet-scan",
        json={"network": "999.0.0.0/8"},
    )

    assert response.status_code == 422


def test_command_missing_required_fields_maps_to_422() -> None:
    client = _client(AppContext(device_control=MagicMock()))

    response = client.post("/api/v1/devices/d1/commands", json={})

    assert response.status_code == 422


def test_unknown_operation_query_returns_404() -> None:
    client = _client(AppContext(operations=OperationRegistry()))

    response = client.get("/api/v1/operations/ghost")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 请求关联
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 配置 Apply 结果语义
# ---------------------------------------------------------------------------


def test_config_apply_validation_failure_maps_to_422() -> None:
    """校验失败是客户端输入问题：422，不允许 200 + success=false 的模糊语义。"""
    from web_server.application.config.files import ConfigApplyResult

    admin = MagicMock()
    admin.apply_file = AsyncMock(
        return_value=ConfigApplyResult(success=False, errors=["yaml: line 1: syntax error"])
    )
    client = _client(AppContext(config_admin=admin))

    response = client.post(
        "/api/v1/config/apply",
        json={"name": "system.yaml", "content": "[invalid"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_config_apply_reload_failure_with_rollback_stays_200() -> None:
    """写入后 reload 失败但已回滚：live 状态未变，200 + success=false 如实上报。"""
    from web_server.application.config.files import ConfigApplyResult

    admin = MagicMock()
    admin.apply_file = AsyncMock(
        return_value=ConfigApplyResult(
            success=False, errors=["worker rejected config"], rollback_performed=True
        )
    )
    client = _client(AppContext(config_admin=admin))

    response = client.post(
        "/api/v1/config/apply",
        json={"name": "system.yaml", "content": "site:\n  site_id: x\n"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["rollback_performed"] is True


def test_every_response_carries_request_id() -> None:
    client = _client(AppContext(operations=OperationRegistry()))

    generated = client.get("/api/v1/operations/ghost")
    assert generated.headers.get("x-request-id")

    echoed = client.get("/api/v1/operations/ghost", headers={"x-request-id": "trace-1"})
    assert echoed.headers["x-request-id"] == "trace-1"


def test_get_config_file_never_returns_plaintext_password() -> None:
    """GET /config/files/{name} 必须经脱敏通道，响应体不得含明文密码。"""
    admin = MagicMock()
    admin.read_file_redacted.return_value = (
        "sinks:\n  redis_main:\n    connection:\n"
        "      password: __REDACTED_SECRET__\n"
    )
    client = _client(AppContext(config_admin=admin))

    response = client.get("/api/v1/config/files/sinks.yaml")

    assert response.status_code == 200
    admin.read_file_redacted.assert_called_once_with("sinks.yaml")
    assert "s3cr3t" not in response.text
    assert "__REDACTED_SECRET__" in response.text
