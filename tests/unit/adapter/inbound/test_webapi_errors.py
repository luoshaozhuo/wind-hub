"""Unit tests for unified API error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from wind_hub.adapter.inbound.webapi.app import build_api
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.application.app_context import AppContext, clear_context, set_context
from wind_hub.domain.model.errors import ProtocolError


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_context()
    yield
    clear_context()


def _client() -> TestClient:
    # raise_server_exceptions=False so a truly-unhandled error yields a 500
    # response (via the generic handler) instead of propagating to the test.
    return TestClient(build_api(), raise_server_exceptions=False)


def _install_query(status_side_effect: object) -> None:
    query = AsyncMock()
    query.status.side_effect = status_side_effect
    set_context(AppContext(command=AsyncMock(), query=query))


def test_api_error_returns_unified_format() -> None:
    """A missing context surfaces as a 503 APIError with the standard envelope."""
    resp = _client().get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert body["error"]["message"]
    assert "details" in body["error"]


def test_windhub_error_returns_unified_format() -> None:
    """A domain ProtocolError maps to 503 with a stable code."""
    _install_query(ProtocolError("connection refused"))
    resp = _client().get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "PROTOCOL_ERROR"
    assert "details" in body["error"]


def test_unhandled_exception_returns_500() -> None:
    """A non-domain exception maps to 500 and hides the traceback."""
    _install_query(RuntimeError("kaboom"))
    resp = _client().get("/health")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"


def test_error_response_contains_no_traceback() -> None:
    _install_query(RuntimeError("secret internal detail"))
    resp = _client().get("/health")
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
