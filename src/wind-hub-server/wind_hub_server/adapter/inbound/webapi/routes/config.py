"""POST /config/reload — hot-reload configuration and report the diff."""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.models import ReloadResponse
from wind_hub.domain.model.reload import ReloadResult

router = APIRouter(tags=["config"])


@router.post("/config/reload", response_model=ReloadResponse)
async def reload_config() -> ReloadResponse:
    """Run a full hot-reload cycle; 503 when the config service is absent."""
    ctx = get_ctx()
    if ctx.config is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "config use case is not configured",
            status_code=503,
        )
    result: ReloadResult = await ctx.config.reload()
    return _to_response(result)


def _to_response(result: ReloadResult) -> ReloadResponse:
    diff = result.diff
    return ReloadResponse(
        success=result.success,
        devices_added=diff.devices.added,
        devices_removed=diff.devices.removed,
        devices_updated=diff.devices.updated,
        sinks_added=diff.sinks.added,
        sinks_removed=diff.sinks.removed,
        sinks_updated=diff.sinks.updated,
        tasks_added=diff.tasks.added,
        tasks_removed=diff.tasks.removed,
        tasks_updated=diff.tasks.updated,
        errors=result.errors,
        duration_ms=result.duration_ms,
        reloaded_at=result.reloaded_at,
    )
