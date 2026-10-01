"""Unified API error handling.

Every failure is returned with a single envelope::

    {"error": {"code": "...", "message": "...", "details": {...}}}

Internal stack traces are logged, not leaked to clients.  Status codes follow
the unified contract (422 validation / 404 not found / 409 conflict /
500 internal / 503 unavailable / 504 timeout). Domain exceptions are mapped to
these codes via a small
lookup table rather than scattered ``try/except`` blocks in the routes.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from wind_hub.domain.model.errors import (
    CommandError,
    ConfigError,
    OperationTimeoutError,
    ProtocolError,
    SinkError,
    WindHubError,
)

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


# -- domain exception → HTTP status / stable code --------------------------
# Walked in insertion order; the first match wins (all are direct subclasses
# of WindHubError, so ordering is only a safety net).
_WINDHUB_ERROR_STATUS: list[tuple[type[WindHubError], int]] = [
    (CommandError, 404),
    (ProtocolError, 503),
    (ConfigError, 400),
    (SinkError, 502),
    (OperationTimeoutError, 504),
]

_WINDHUB_ERROR_CODES: list[tuple[type[WindHubError], str]] = [
    (CommandError, "COMMAND_FAILED"),
    (ProtocolError, "PROTOCOL_ERROR"),
    (ConfigError, "CONFIG_ERROR"),
    (SinkError, "SINK_ERROR"),
    (OperationTimeoutError, "OPERATION_TIMEOUT"),
]


def _status_for(exc: WindHubError) -> int:
    for cls, status in _WINDHUB_ERROR_STATUS:
        if isinstance(exc, cls):
            return status
    return 500


def _code_for(exc: WindHubError) -> str:
    for cls, code in _WINDHUB_ERROR_CODES:
        if isinstance(exc, cls):
            return code
    return "WIND_HUB_ERROR"


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


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Serialize a domain exception into the unified envelope."""
    err = cast(WindHubError, exc)
    details: dict[str, Any] = {}
    if isinstance(err, CommandError):
        details["command_id"] = err.command_id
    return _envelope(_status_for(err), _code_for(err), str(err), details)


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
    app.add_exception_handler(WindHubError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, generic_error_handler)


__all__ = [
    "APIError",
    "api_error_handler",
    "domain_error_handler",
    "generic_error_handler",
    "validation_error_handler",
    "register_error_handlers",
]
