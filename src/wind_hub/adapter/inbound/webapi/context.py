"""Web API access to the shared :class:`AppContext`.

The context itself lives in the application layer
(:mod:`wind_hub.application.app_context`) and is shared by the CLI and Web
API adapters.  This module adds ``get_ctx``, which turns a missing context
into a 503 rather than a ``RuntimeError``.
"""

from __future__ import annotations

from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.application.app_context import (
    AppContext,
    clear_context,
    get_context,
    set_context,
)

__all__ = ["AppContext", "set_context", "get_context", "clear_context", "get_ctx"]


def get_ctx() -> AppContext:
    """Return the application context, or raise a 503 ``APIError`` when unset."""
    try:
        return get_context()
    except RuntimeError as exc:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "Application context is not set — the engine has not started",
            status_code=503,
        ) from exc
