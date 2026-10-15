"""Admin API v1 Operations 路由：异步 Operation 快照查询。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from web_server.adapter.inbound.webapi.context import get_ctx
from web_server.adapter.inbound.webapi.errors import APIError
from web_server.adapter.inbound.webapi.v1 import common
from web_server.adapter.inbound.webapi.v1.models import OperationResponse
from web_server.application.app_context import AppContext
from web_server.infra.audit import audit

router = APIRouter()


@router.get("/operations/{operation_id}", response_model=OperationResponse, tags=["v1-operations"])
async def get_operation(
    operation_id: str,
    ctx: AppContext = Depends(get_ctx),
) -> OperationResponse:
    """查询异步 Operation 快照。"""
    manager = ctx.operations
    if manager is None:
        raise APIError("SERVICE_UNAVAILABLE", "operation manager is not configured", 503)
    try:
        row = manager.get(operation_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown operation '{operation_id}'", 404) from None
    return common.operation_response(row)


@router.post(
    "/operations/{operation_id}/cancel",
    response_model=OperationResponse,
    tags=["v1-operations"],
)
async def cancel_operation(
    operation_id: str,
    ctx: AppContext = Depends(get_ctx),
) -> OperationResponse:
    """取消运行中的诊断 Operation（终止底层协程，而非仅改状态）。"""
    diagnostics = common.diagnostics(ctx)
    try:
        row = diagnostics.cancel_operation(operation_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown operation '{operation_id}'", 404) from None
    except ValueError as exc:
        raise APIError("OPERATION_NOT_CANCELLABLE", str(exc), 409) from exc
    audit("operation.cancel", operation_id=operation_id, kind=row.kind)
    return common.operation_response(row)
