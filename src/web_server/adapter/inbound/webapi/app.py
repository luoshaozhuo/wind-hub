"""Web API entry point — assembles the FastAPI application."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from web_server.adapter.inbound.webapi import errors
from web_server.adapter.inbound.webapi.routes import metrics
from web_server.adapter.inbound.webapi.v1.router import router as v1_router
from web_server.application.app_context import AppContext
from web_server.infra.correlation import RequestIdMiddleware


def build_api(context: AppContext) -> FastAPI:
    """Build the ``wind-hub`` FastAPI application.

    ``context`` 由组合根（``server.py``）装配后显式注入，经 ``app.state``
    传递给请求级依赖 ``get_ctx``；不存在进程级共享 context。

    管理 API 统一挂载在 ``/api/v1``；``/metrics`` 保留为 Prometheus 标准入口。
    错误处理统一在 ``errors.py``。OpenAPI docs are generated automatically at
    ``/docs``, ``/redoc``, and ``/openapi.json``.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # 进程关闭时取消诊断后台任务，避免悬挂协程阻塞退出或泄漏资源。
        diagnostics = app.state.app_context.diagnostics
        if diagnostics is not None:
            await diagnostics.aclose()

    app = FastAPI(
        title="wind-hub",
        description="风电场主控通信模块",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.app_context = context
    app.add_middleware(RequestIdMiddleware)
    errors.register_error_handlers(app)
    app.include_router(v1_router)
    app.include_router(metrics.router)
    return app


__all__ = ["build_api"]
