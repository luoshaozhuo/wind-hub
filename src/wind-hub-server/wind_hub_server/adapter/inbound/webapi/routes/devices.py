"""GET /devices and GET /devices/{device_id} — 兼容设备状态入口。"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.models import DeviceInfoResponse
from wind_hub_server.application.usecase.device import DeviceSnapshot

router = APIRouter(tags=["devices"])


def _to_response(info: DeviceSnapshot) -> DeviceInfoResponse:
    return DeviceInfoResponse(
        device_id=info.device_id,
        protocol=info.protocol,
        connected=info.connected,
        last_seen=None,
    )


@router.get("/devices", response_model=list[DeviceInfoResponse])
async def list_devices() -> list[DeviceInfoResponse]:
    """列出配置设备及 Collector 当前连接状态。"""
    ctx = get_ctx()
    if ctx.devices is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "devices use case is not configured",
            status_code=503,
        )
    infos = await ctx.devices.list_devices()
    return [_to_response(info) for info in infos]


@router.get("/devices/{device_id}", response_model=DeviceInfoResponse)
async def get_device(device_id: str) -> DeviceInfoResponse:
    """返回单设备状态；未知设备映射为 404。"""
    ctx = get_ctx()
    if ctx.devices is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "devices use case is not configured",
            status_code=503,
        )
    try:
        info = await ctx.devices.get_device(device_id)
    except KeyError:
        raise APIError(
            "NOT_FOUND",
            f"unknown device '{device_id}'",
            status_code=404,
        ) from None
    return _to_response(info)
