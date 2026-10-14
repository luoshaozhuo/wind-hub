"""Web API 的 :class:`AppContext` 显式注入入口。

context 由组合根装配后经 ``build_api(context)`` 挂到 FastAPI ``app.state``；
本模块提供请求级依赖 ``get_ctx``，把缺失上下文映射为 HTTP 503。application
层不感知 FastAPI；进程级 global service locator 已移除。
"""

from __future__ import annotations

from fastapi import Request

from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.application.app_context import AppContext

__all__ = ["AppContext", "get_ctx"]


def get_ctx(request: Request) -> AppContext:
    """Return the application context, or raise a 503 ``APIError`` when unset."""
    ctx: AppContext | None = getattr(request.app.state, "app_context", None)
    if ctx is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "Application context is not set — the engine has not started",
            status_code=503,
        )
    return ctx
