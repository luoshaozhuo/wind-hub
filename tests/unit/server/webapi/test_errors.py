"""Unit tests for unified API error handling."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from wind_hub_core.model.errors import ProtocolError
from wind_hub_server.adapter.inbound.webapi.app import build_api
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.application.app_context import AppContext, clear_context, set_context


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_context()
    yield
    clear_context()


def _client() -> TestClient:
    # raise_server_exceptions=False so a truly-unhandled error yields a 500
    # response (via the generic handler) instead of propagating to the test.
    return TestClient(build_api(), raise_server_exceptions=False)


def _install_overview(side_effect: object) -> None:
    """安装 overview 用例，其 ``snapshot`` 以 ``side_effect`` 失败。

    错误包络契约经任一真实路由验证；此处选用 ``GET /api/v1/overview``
    （直达 use case，无额外参数校验分支）。
    """
    overview = MagicMock()
    overview.snapshot.side_effect = side_effect
    set_context(AppContext(overview=overview))


def test_missing_service_returns_unified_format() -> None:
    """未配置的用例经 APIError 映射为 503 标准包络。"""
    set_context(AppContext())
    resp = _client().get("/api/v1/overview")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert body["error"]["message"]
    assert "details" in body["error"]


def test_windhub_error_returns_unified_format() -> None:
    """A domain ProtocolError maps to 503 with a stable code."""
    _install_overview(ProtocolError("connection refused"))
    resp = _client().get("/api/v1/overview")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "PROTOCOL_ERROR"
    assert "details" in body["error"]


def test_unhandled_exception_returns_500() -> None:
    """A non-domain exception maps to 500 and hides the traceback."""
    _install_overview(RuntimeError("kaboom"))
    resp = _client().get("/api/v1/overview")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"


def test_error_response_contains_no_traceback() -> None:
    _install_overview(RuntimeError("secret internal detail"))
    resp = _client().get("/api/v1/overview")
    # The raw exception message and any traceback must not leak to the client.
    assert "secret internal detail" not in resp.text
    assert "Traceback" not in resp.text
    assert "kaboom" not in resp.text


def test_api_error_direct_construction() -> None:
    """APIError carries a stable code and status code for callers."""
    exc = APIError("NOT_FOUND", "missing", status_code=404, details={"device_id": "d1"})
    assert exc.code == "NOT_FOUND"
    assert exc.status_code == 404
    assert exc.details == {"device_id": "d1"}
