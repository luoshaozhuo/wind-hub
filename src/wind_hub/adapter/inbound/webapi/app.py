"""Web API entry point — assembles the FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from wind_hub.adapter.inbound.webapi import errors
from wind_hub.adapter.inbound.webapi.context import get_context
from wind_hub.adapter.inbound.webapi.routes import (
    commands,
    config,
    devices,
    health,
    metrics,
    points,
)
from wind_hub.adapter.inbound.webapi.routes import (
    routes as explain_routes,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify the ``AppContext`` is set before serving requests.

    ``main.py`` 在启动 uvicorn 前调用 ``set_context``；若上下文缺失（例如
    误用启动顺序），这里在监听前就失败，而不是让每个请求都返回 503。
    """
    get_context()
    yield


def build_api() -> FastAPI:
    """Build the ``wind-hub`` FastAPI application.

    Routes are grouped by resource in ``routes/*.py``; error handling is
    unified in ``errors.py``.  OpenAPI docs are generated automatically at
    ``/docs``, ``/redoc``, and ``/openapi.json``.
    """
    app = FastAPI(
        title="wind-hub",
        description="风电场主控通信模块",
        version="0.1.0",
        lifespan=lifespan,
    )
    errors.register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(devices.router)
    app.include_router(points.router)
    app.include_router(commands.router)
    app.include_router(config.router)
    app.include_router(explain_routes.router)
    app.include_router(metrics.router)
    return app


__all__ = ["build_api"]
