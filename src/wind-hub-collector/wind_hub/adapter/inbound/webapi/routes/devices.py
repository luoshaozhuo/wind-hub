"""GET /devices and GET /devices/{device_id} — device list and detail."""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.models import DeviceInfoResponse
from wind_hub.domain.model.device import DeviceInfo
from wind_hub.domain.model.errors import CommandError

router = APIRouter(tags=["devices"])


def _to_response(info: DeviceInfo) -> DeviceInfoResponse:
    return DeviceInfoResponse(
        device_id=info.device_id,
        protocol=info.protocol,
        connected=info.connected,
        last_seen=info.last_seen,
    )


@router.get("/devices", response_model=list[DeviceInfoResponse])
async def list_devices() -> list[DeviceInfoResponse]:
    """List all configured devices and their runtime status."""
    ctx = get_ctx()
    if ctx.query is None:
        raise APIError("SERVICE_UNAVAILABLE", "query use case is not configured", status_code=503)
    infos = await ctx.query.list_devices()
    return [_to_response(info) for info in infos]


@router.get("/devices/{device_id}", response_model=DeviceInfoResponse)
async def get_device(device_id: str) -> DeviceInfoResponse:
    """Return the runtime status of a single device (404 when unknown)."""
    ctx = get_ctx()
    if ctx.query is None:
        raise APIError("SERVICE_UNAVAILABLE", "query use case is not configured", status_code=503)
    try:
        info = await ctx.query.get_device_info(device_id)
    except CommandError as exc:
        raise APIError("NOT_FOUND", str(exc), status_code=404) from exc
    return _to_response(info)
