"""GET /points/{device_id}/{point_id} — read a single point's current value."""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.models import PointValueResponse
from wind_hub.domain.model.errors import CommandError
from wind_hub.domain.model.point import PointValue

router = APIRouter(tags=["points"])


@router.get("/points/{device_id}/{point_id}", response_model=PointValueResponse)
async def read_point(device_id: str, point_id: str) -> PointValueResponse:
    """Read a single point's value (404 unknown, 503 device unreachable)."""
    ctx = get_ctx()
    if ctx.query_service is None:
        raise APIError("SERVICE_UNAVAILABLE", "query_service is not configured", status_code=503)
    try:
        value = await ctx.query_service.read_point(device_id, point_id)
    except CommandError as exc:
        raise APIError("NOT_FOUND", str(exc), status_code=404) from exc
    # ProtocolError propagates to the domain error handler → 503.
    return _to_response(value)


def _to_response(value: PointValue) -> PointValueResponse:
    return PointValueResponse(
        device_id=value.device_id,
        point_id=value.point_id,
        value=value.value,
        quality=value.quality.value,
        timestamp=value.timestamp,
        source=value.source,
    )
