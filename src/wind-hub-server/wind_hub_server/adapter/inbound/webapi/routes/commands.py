"""POST /commands — 兼容单点写命令入口。"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.models import CommandRequest, CommandResponse

router = APIRouter(tags=["commands"])


@router.post("/commands", response_model=CommandResponse)
async def send_command(request: CommandRequest) -> CommandResponse:
    """通过 Server DeviceControl → Commander 执行单点写入。"""
    ctx = get_ctx()
    if ctx.device_control is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            "device control use case is not configured",
            status_code=503,
        )
    result = await ctx.device_control.send(
        request.device_id,
        request.point_id,
        request.value,
        timeout=request.timeout,
        command_id=request.command_id,
    )
    return CommandResponse(
        command_id=result.command_id,
        success=result.success,
        error=result.error,
        finished_at=result.finished_at,
    )
