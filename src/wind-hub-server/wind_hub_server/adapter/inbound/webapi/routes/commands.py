"""POST /commands — issue a single write command and return the result."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.models import CommandRequest, CommandResponse
from wind_hub_core.model.command import Command, CommandResult

router = APIRouter(tags=["commands"])


@router.post("/commands", response_model=CommandResponse)
async def send_command(request: CommandRequest) -> CommandResponse:
    """Issue a write command and wait for the ``CommandResult``.

    The idempotency ``command_id`` is generated server-side when omitted.
    A protocol-level failure is reported inline via ``success=False``;
    a dispatch failure (e.g. unknown device) raises ``CommandError`` → 404.
    """
    ctx = get_ctx()
    if ctx.command is None:
        raise APIError("SERVICE_UNAVAILABLE", "command use case is not configured", status_code=503)
    command = Command(
        command_id=request.command_id or str(uuid.uuid4()),
        device_id=request.device_id,
        point_id=request.point_id,
        value=request.value,
        timeout=request.timeout,
    )
    result = await ctx.command.send(command)
    return _to_response(result)


def _to_response(result: CommandResult) -> CommandResponse:
    return CommandResponse(
        command_id=result.command_id,
        success=result.success,
        error=result.error,
        finished_at=result.finished_at,
    )
