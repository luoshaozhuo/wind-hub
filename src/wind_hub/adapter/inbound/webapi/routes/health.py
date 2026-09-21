"""GET /health — engine health snapshot."""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.models import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Return a health snapshot derived from ``QueryUseCase.status()``."""
    ctx = get_ctx()
    if ctx.query is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "query_service is not configured",
            status_code=503,
        )
    snapshot = await ctx.query.status()
    return HealthResponse(
        status="ok" if snapshot.running else "down",
        running=snapshot.running,
        device_count=snapshot.device_count,
        sink_count=snapshot.sink_count,
        devices_connected=snapshot.devices_connected,
        sinks_healthy=snapshot.sinks_healthy,
        points_collected=snapshot.points_collected,
        points_routed=snapshot.points_routed,
        points_dropped=snapshot.points_dropped,
    )
