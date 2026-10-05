"""Admin API v1 Devices 路由：配置查询、Data/Trend 与 Command。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    DeviceCommandRequest,
    DeviceCommandResponse,
    DeviceDataItemResponse,
    DeviceDataPageResponse,
    DevicePageResponse,
    DeviceResponse,
    TrendSeriesResponse,
)

router = APIRouter()


@router.get("/devices", response_model=DevicePageResponse, tags=["v1-devices"])
async def list_devices(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None),
) -> DevicePageResponse:
    """分页查询设备；搜索作用于完整结果集后再分页。"""
    rows = common.devices().list_devices(search)
    paged, meta = common.page(rows, page, page_size)
    return DevicePageResponse(
        items=[common.device_response(row) for row in paged],
        page=meta,
    )


@router.get("/devices/{device_id}", response_model=DeviceResponse, tags=["v1-devices"])
async def get_device(device_id: str) -> DeviceResponse:
    """查询单设备静态配置与实时连接状态。"""
    try:
        row = common.devices().get_device(device_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown device '{device_id}'", 404) from None
    return common.device_response(row)


@router.get(
    "/devices/{device_id}/data",
    response_model=DeviceDataPageResponse,
    tags=["v1-devices"],
)
async def get_device_data(
    device_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    search: str | None = Query(None),
    point_group: str | None = Query(None),
) -> DeviceDataPageResponse:
    """经 Commander 即时读取设备当前点值，并返回点定义 metadata。"""
    try:
        rows = await common.device_data().list_data(
            device_id, search=search, point_group=point_group
        )
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown device '{device_id}'", 404) from None
    paged, meta = common.page(rows, page, page_size)
    return DeviceDataPageResponse(
        items=[
            DeviceDataItemResponse(
                **{
                    **row.model_dump(),
                    "quality": row.quality.value if row.quality is not None else None,
                }
            )
            for row in paged
        ],
        page=meta,
    )


@router.get(
    "/devices/{device_id}/trend",
    response_model=list[TrendSeriesResponse],
    tags=["v1-devices"],
)
async def get_device_trend(
    device_id: str,
    point_id: list[str] = Query(...),
    window_seconds: int = Query(600, ge=1, le=604800),
    limit_per_point: int = Query(600, ge=1, le=3600),
) -> list[TrendSeriesResponse]:
    """即时补采当前值并查询 Server 进程内短期趋势；不访问历史数据库。"""
    try:
        series = await common.device_data().trend(
            device_id,
            point_id,
            window_seconds=window_seconds,
            limit_per_point=limit_per_point,
        )
    except KeyError as exc:
        raise APIError("NOT_FOUND", str(exc), 404) from exc
    return [common.trend_response(row) for row in series]


@router.post(
    "/devices/{device_id}/commands",
    response_model=DeviceCommandResponse,
    tags=["v1-devices"],
)
async def send_device_command(
    device_id: str, request: DeviceCommandRequest
) -> DeviceCommandResponse:
    """真实写入设备；写成功后即时回读并追加短期 Trend 样本。"""
    result = await common.device_control().send(
        device_id,
        request.point_id,
        request.value,
        timeout=request.timeout,
        command_id=request.command_id,
    )
    return DeviceCommandResponse(**result.model_dump())
