"""Admin API v1 Operations 路由：异步 Operation 快照查询。"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import OperationResponse

router = APIRouter()


@router.get("/operations/{operation_id}", response_model=OperationResponse, tags=["v1-operations"])
async def get_operation(operation_id: str) -> OperationResponse:
    """查询异步 Operation 快照。"""
    manager = get_ctx().operations
    if manager is None:
        raise APIError("SERVICE_UNAVAILABLE", "operation manager is not configured", 503)
    try:
        row = manager.get(operation_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown operation '{operation_id}'", 404) from None
    return common.operation_response(row)
