"""Admin API v1 Monitoring 路由：Overview、Quality、Logs 与 System Health。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    LogEntryResponse,
    LogPageResponse,
    OverviewResponse,
    PageMeta,
    QualityResponse,
    SystemHealthResponse,
)
from wind_hub_server.application.app_context import AppContext
from wind_hub_server.application.monitoring.health import HealthRange
from wind_hub_server.application.monitoring.overview import OverviewSnapshot
from wind_hub_server.application.monitoring.quality import QualityWindow

router = APIRouter()


@router.get("/overview", response_model=OverviewResponse, tags=["v1-overview"])
async def get_overview(ctx: AppContext = Depends(get_ctx)) -> OverviewResponse:
    """返回 wind-hub-admin 总览页的一次聚合运行快照。"""
    snapshot: OverviewSnapshot = common.overview(ctx).snapshot()
    return OverviewResponse(**snapshot.model_dump())


@router.get("/quality", response_model=QualityResponse, tags=["v1-quality"])
async def get_quality(
    window: QualityWindow = Query("24h"),
    ctx: AppContext = Depends(get_ctx),
) -> QualityResponse:
    snapshot = await common.quality(ctx).snapshot(window)
    return QualityResponse(**snapshot.model_dump())


@router.post("/quality/check", response_model=QualityResponse, tags=["v1-quality"])
async def run_quality_check(
    window: QualityWindow = Query("24h"),
    ctx: AppContext = Depends(get_ctx),
) -> QualityResponse:
    """立即刷新 Collector 监控快照并按窗口重算。"""
    snapshot = await common.quality(ctx).snapshot(window, refresh=True)
    return QualityResponse(**snapshot.model_dump())


@router.get("/logs", response_model=LogPageResponse, tags=["v1-logs"])
async def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    level: str | None = Query(None),
    source: str | None = Query(None),
    keyword: str | None = Query(None),
    ctx: AppContext = Depends(get_ctx),
) -> LogPageResponse:
    result = common.logs(ctx).list_logs(
        page=page, page_size=page_size, level=level, source=source, keyword=keyword
    )
    return LogPageResponse(
        items=[LogEntryResponse(**row.model_dump()) for row in result.items],
        page=PageMeta(page=result.page, page_size=result.page_size, total=result.total),
    )


@router.get("/logs/sources", response_model=list[str], tags=["v1-logs"])
async def list_log_sources(ctx: AppContext = Depends(get_ctx)) -> list[str]:
    return common.logs(ctx).sources()


@router.get(
    "/system-health",
    response_model=SystemHealthResponse,
    tags=["v1-system-health"],
)
async def get_system_health(
    range_name: HealthRange = Query("24h", alias="range"),
    ctx: AppContext = Depends(get_ctx),
) -> SystemHealthResponse:
    snapshot = common.system_health(ctx).snapshot(range_name)
    return SystemHealthResponse(**snapshot.model_dump())
