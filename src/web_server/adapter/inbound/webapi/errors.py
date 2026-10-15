"""Unified API error handling.

Every failure is returned with a single envelope::

    {"error": {"code": "...", "message": "...", "details": {...}}}

Internal stack traces are logged, not leaked to clients.  Status codes follow
the unified contract (422 validation / 404 not found / 409 conflict /
500 internal / 503 unavailable / 504 timeout).  Domain and transport
exceptions are mapped to these codes via small lookup tables rather than
scattered ``try/except`` blocks in the routes.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import grpc
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.application.errors import ConfigError, CoreError, ProtocolError

logger = logging.getLogger(__name__)


class APIError(Exception):
    """API-layer error carrying a stable ``code`` and an HTTP ``status_code``."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


# -- core domain exception → HTTP status / stable code ----------------------
_CORE_ERROR_MAP: list[tuple[type[CoreError], int, str]] = [
    (ConfigError, 400, "CONFIG_ERROR"),
    (ProtocolError, 503, "PROTOCOL_ERROR"),
]

# -- gRPC transport status → HTTP status / stable code ----------------------
# Worker（Collector/Commander）不可达与超时是正常故障形态，不得落入 500。
_GRPC_STATUS_MAP: dict[grpc.StatusCode, tuple[int, str]] = {
    grpc.StatusCode.UNAVAILABLE: (503, "WORKER_UNAVAILABLE"),
    grpc.StatusCode.DEADLINE_EXCEEDED: (504, "WORKER_TIMEOUT"),
    grpc.StatusCode.CANCELLED: (503, "WORKER_CALL_CANCELLED"),
    grpc.StatusCode.NOT_FOUND: (404, "NOT_FOUND"),
    grpc.StatusCode.INVALID_ARGUMENT: (422, "INVALID_ARGUMENT"),
    grpc.StatusCode.FAILED_PRECONDITION: (409, "PRECONDITION_FAILED"),
    grpc.StatusCode.PERMISSION_DENIED: (403, "PERMISSION_DENIED"),
    grpc.StatusCode.RESOURCE_EXHAUSTED: (429, "RESOURCE_EXHAUSTED"),
}


def _envelope(status_code: int, code: str, message: str, details: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


# Starlette's ``add_exception_handler`` requires handlers typed as
# ``(Request, Exception)``; the concrete exception class is selected by the
# registration call below, so the ``cast`` here is safe — the framework never
# dispatches an ``APIError`` handler with a non-APIError instance.
async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Serialize an :class:`APIError` into the unified envelope."""
    err = cast(APIError, exc)
    return _envelope(err.status_code, err.code, err.message, err.details)


async def core_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Serialize a shared-core domain exception into the unified envelope."""
    err = cast(CoreError, exc)
    for cls, status, code in _CORE_ERROR_MAP:
        if isinstance(err, cls):
            return _envelope(status, code, str(err), {})
    return _envelope(500, "CORE_ERROR", str(err), {})


async def grpc_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map worker gRPC transport failures to explicit 503/504 style codes."""
    err = cast(grpc.aio.AioRpcError, exc)
    status, code = _GRPC_STATUS_MAP.get(err.code(), (502, "WORKER_RPC_ERROR"))
    logger.warning(
        "gRPC call failed for %s %s: %s %s",
        request.method,
        request.url.path,
        err.code(),
        err.details(),
    )
    return _envelope(status, code, err.details() or code, {})


async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for unexpected exceptions — log, never leak the traceback."""
    logger.exception("Unhandled error while serving %s %s", request.method, request.url.path)
    return _envelope(500, "INTERNAL_ERROR", "Internal server error", {})


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map request-format errors to HTTP 422 with machine-readable details."""
    err = cast(RequestValidationError, exc)
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in err.errors()
    ]
    return _envelope(422, "VALIDATION_ERROR", "Invalid request", {"errors": errors})


def register_error_handlers(app: FastAPI) -> None:
    """Attach all error handlers to a FastAPI application."""
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(CoreError, core_error_handler)
    app.add_exception_handler(grpc.aio.AioRpcError, grpc_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, generic_error_handler)


__all__ = [
    "APIError",
    "api_error_handler",
    "core_error_handler",
    "grpc_error_handler",
    "generic_error_handler",
    "validation_error_handler",
    "register_error_handlers",
]
