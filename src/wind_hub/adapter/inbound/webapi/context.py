"""Web API access to the shared :class:`AppContext`.

The context itself lives in the CLI adapter
(:mod:`wind_hub.adapter.inbound.cli.context`) so both adapters share a single
instance.  This module adds ``get_ctx``, which turns a missing context into a
503 rather than the CLI's ``RuntimeError``.
"""

from __future__ import annotations

from wind_hub.adapter.inbound.cli.context import (
    AppContext,
    clear_context,
    get_context,
    set_context,
)
from wind_hub.adapter.inbound.webapi.errors import APIError

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
