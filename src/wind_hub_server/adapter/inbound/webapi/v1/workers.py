"""Admin API v1 Workers 路由：已登记 Worker 的最近探测状态。"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import WorkerResponse

router = APIRouter()


@router.get("/workers", response_model=list[WorkerResponse], tags=["v1-workers"])
async def list_workers() -> list[WorkerResponse]:
    """返回 Server 当前已登记 Worker 的最近探测状态。"""
    rows = await common.workers().list_workers()
    return [WorkerResponse(**row.model_dump(mode="json")) for row in rows]


@router.get(
    "/workers/{worker_id}",
    response_model=WorkerResponse,
    tags=["v1-workers"],
)
async def get_worker(worker_id: str) -> WorkerResponse:
    """返回指定 Worker 的最近探测状态。"""
    try:
        row = await common.workers().get_worker(worker_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown worker '{worker_id}'", 404) from None
    return WorkerResponse(**row.model_dump(mode="json"))
