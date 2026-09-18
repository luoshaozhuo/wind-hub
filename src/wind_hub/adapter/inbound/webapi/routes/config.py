"""POST /config/reload — hot-reload configuration and report the diff."""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.models import ReloadResponse
from wind_hub.domain.model.reload import ReloadResult

router = APIRouter(tags=["config"])


@router.post("/config/reload", response_model=ReloadResponse)
async def reload_config() -> ReloadResponse:
    """Run a full hot-reload cycle; 503 when the config service is absent."""
    ctx = get_ctx()
    if ctx.config_service is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "config_service is not configured",
            status_code=503,
        )
    result: ReloadResult = await ctx.config_service.reload()
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
        routing_rebuilt=diff.points_changed or diff.rules_changed,
        pipeline_rebuilt=diff.pipeline_changed,
        errors=result.errors,
        duration_ms=result.duration_ms,
        reloaded_at=result.reloaded_at,
    )
