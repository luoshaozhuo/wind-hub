"""Contract：REST API 统一错误信封与状态码映射。

对外稳定契约：任何失败都返回
``{"error": {"code", "message", "details"}}``，状态码映射表（422/404/
409/500/503/504/502）不得漂移，500 不得泄漏 traceback。前端与第三方
集成依赖该信封做错误分派——这是 API 契约测试，不是单条路由的单元测试。

用 in-process ASGI 客户端 + 可编程假 UseCase 触发各域异常，不监听端口。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from wind_hub_core.model.errors import (
    CommandError,
    ConfigError,
    OperationTimeoutError,
    ProtocolError,
    SinkError,
)
from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.application.app_context import (
    AppContext,
    clear_context,
    set_context,
)


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_context()
    yield
    clear_context()


def _client_with_overview(side_effect: BaseException) -> TestClient:
    overview = AsyncMock()
    overview.snapshot.side_effect = side_effect
    set_context(AppContext(overview=overview))
    return TestClient(build_api(), raise_server_exceptions=False)


def _assert_envelope(payload: dict[str, Any], code: str) -> None:
    """契约形状：恰好一层 error 包装，含稳定的三个字段。"""
    assert set(payload) == {"error"}, f"envelope must only contain 'error': {payload}"
    error = payload["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)


class TestDomainExceptionMapping:
    """域异常 → (status, code) 映射表是全系统对外契约。"""

    @pytest.mark.parametrize(
        ("exc", "status", "code"),
        [
            (CommandError("write failed", command_id="c-1"), 404, "COMMAND_FAILED"),
            (ProtocolError("device unreachable"), 503, "PROTOCOL_ERROR"),
            (ConfigError("bad config"), 400, "CONFIG_ERROR"),
            (SinkError("broker down"), 502, "SINK_ERROR"),
            (OperationTimeoutError("timed out"), 504, "OPERATION_TIMEOUT"),
        ],
    )
    def test_domain_error_envelope(
        self, exc: BaseException, status: int, code: str
    ) -> None:
        client = _client_with_overview(exc)
        response = client.get("/api/v1/overview")

        assert response.status_code == status
        _assert_envelope(response.json(), code)

    def test_command_error_carries_command_id_in_details(self) -> None:
        client = _client_with_overview(CommandError("write failed", command_id="c-9"))
        response = client.get("/api/v1/overview")

        assert response.json()["error"]["details"]["command_id"] == "c-9"

    def test_unknown_exception_is_500_without_traceback(self) -> None:
        client = _client_with_overview(RuntimeError("secret internal detail"))
        response = client.get("/api/v1/overview")

        assert response.status_code == 500
        payload = response.json()
        _assert_envelope(payload, "INTERNAL_ERROR")
        # 内部细节与堆栈不得泄漏到响应体。
        assert "secret internal detail" not in response.text
        assert "Traceback" not in response.text


class TestValidationErrorEnvelope:
    def test_missing_request_body_is_422_with_machine_readable_details(
        self,
    ) -> None:
        set_context(AppContext())
        client = TestClient(build_api(), raise_server_exceptions=False)

        response = client.post("/api/v1/config/validate", json={})

        assert response.status_code == 422
        payload = response.json()
        _assert_envelope(payload, "VALIDATION_ERROR")
        errors = payload["error"]["details"]["errors"]
        assert errors, "validation details must enumerate violations"
        assert all({"loc", "msg", "type"} <= set(e) for e in errors)

    def test_wrong_field_type_is_422(self) -> None:
        set_context(AppContext())
        client = TestClient(build_api(), raise_server_exceptions=False)

        response = client.post(
            "/api/v1/config/validate",
            json={"name": 123, "content": "x"},
        )

        assert response.status_code == 422
        _assert_envelope(response.json(), "VALIDATION_ERROR")


class TestEnvelopeStability:
    def test_404_unknown_task_uses_unified_envelope(self) -> None:
        tasks = AsyncMock()
        tasks.get_task_summary.side_effect = KeyError("ghost")
        set_context(AppContext(tasks=tasks))
        client = TestClient(build_api(), raise_server_exceptions=False)

        response = client.get("/api/v1/tasks/ghost")

        assert response.status_code == 404
        _assert_envelope(response.json(), "NOT_FOUND")

    def test_error_responses_are_json(self) -> None:
        client = _client_with_overview(ProtocolError("down"))
        response = client.get("/api/v1/overview")

        assert response.headers["content-type"].startswith("application/json")
