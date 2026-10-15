"""Request-ID 关联链路测试：HTTP 中间件、日志过滤与 gRPC 传播。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient

from web_server.adapter.inbound.webapi.app import build_api
from web_server.application.app_context import AppContext
from web_server.infra.correlation import (
    RequestIdFilter,
    RequestIdInterceptor,
    get_request_id,
)


async def test_middleware_generates_and_echoes_request_id() -> None:
    app = build_api(AppContext())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/devices")
    request_id = response.headers.get("x-request-id")
    assert request_id
    # 客户端显式携带时透传同一值。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/devices", headers={"x-request-id": "trace-42"})
    assert response.headers["x-request-id"] == "trace-42"


def test_logging_filter_injects_request_id() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (), None)
    RequestIdFilter().filter(record)
    assert record.request_id == "-"  # 无请求上下文


async def test_grpc_interceptor_appends_metadata() -> None:
    interceptor = RequestIdInterceptor()
    captured: dict[str, Any] = {}

    async def continuation(details: Any, request: Any) -> str:
        captured["metadata"] = details.metadata
        return "ok"

    from web_server.infra.correlation import request_id_var

    token = request_id_var.set("trace-99")
    try:
        details = SimpleNamespace(metadata=None, _replace=lambda **kw: SimpleNamespace(**kw))
        result = await interceptor.intercept_unary_unary(continuation, details, object())
    finally:
        request_id_var.reset(token)

    assert result == "ok"
    assert ("x-request-id", "trace-99") in captured["metadata"]
    assert get_request_id() == ""
