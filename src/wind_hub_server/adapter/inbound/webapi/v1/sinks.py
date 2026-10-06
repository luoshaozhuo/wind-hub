"""Admin API v1 Sinks 路由：查询、配置写入、Verify 与 Write Test。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    ConfigApplyResponse,
    SinkResponse,
    SinkTestResponse,
    SinkUpsertRequest,
)
from wind_hub_server.application.app_context import AppContext

router = APIRouter()


@router.get("/sinks", response_model=list[SinkResponse], tags=["v1-sinks"])
async def list_sinks(ctx: AppContext = Depends(get_ctx)) -> list[SinkResponse]:
    return [SinkResponse(**row.model_dump()) for row in common.sinks(ctx).list_sinks()]


@router.get("/sinks/{name}", response_model=SinkResponse, tags=["v1-sinks"])
async def get_sink(name: str, ctx: AppContext = Depends(get_ctx)) -> SinkResponse:
    try:
        row = common.sinks(ctx).get_sink(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkResponse(**row.model_dump())


@router.put("/sinks/{name}", response_model=ConfigApplyResponse, tags=["v1-sinks"])
async def upsert_sink(
    name: str,
    request: SinkUpsertRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ConfigApplyResponse:
    try:
        result = await common.sinks(ctx).upsert(name, request.model_dump())
    except ValueError as exc:
        raise APIError("VALIDATION_ERROR", str(exc), 422) from exc
    return ConfigApplyResponse(**result.model_dump())


@router.delete("/sinks/{name}", response_model=ConfigApplyResponse, tags=["v1-sinks"])
async def delete_sink(name: str, ctx: AppContext = Depends(get_ctx)) -> ConfigApplyResponse:
    try:
        result = await common.sinks(ctx).delete(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return ConfigApplyResponse(**result.model_dump())


@router.post("/sinks/{name}/verify", response_model=SinkTestResponse, tags=["v1-sinks"])
async def verify_sink(name: str, ctx: AppContext = Depends(get_ctx)) -> SinkTestResponse:
    try:
        result = await common.sinks(ctx).verify(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkTestResponse(**result.model_dump())


@router.post(
    "/sinks/{name}/write-test",
    response_model=SinkTestResponse,
    tags=["v1-sinks"],
)
async def sink_write_test(name: str, ctx: AppContext = Depends(get_ctx)) -> SinkTestResponse:
    try:
        result = await common.sinks(ctx).write_test(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkTestResponse(**result.model_dump())
