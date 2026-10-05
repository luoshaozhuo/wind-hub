"""Admin API v1 Sinks 路由：查询、配置写入、Verify 与 Write Test。"""

from __future__ import annotations

from fastapi import APIRouter

from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    ConfigApplyResponse,
    SinkResponse,
    SinkTestResponse,
    SinkUpsertRequest,
)

router = APIRouter()


@router.get("/sinks", response_model=list[SinkResponse], tags=["v1-sinks"])
async def list_sinks() -> list[SinkResponse]:
    return [SinkResponse(**row.model_dump()) for row in common.sinks().list_sinks()]


@router.get("/sinks/{name}", response_model=SinkResponse, tags=["v1-sinks"])
async def get_sink(name: str) -> SinkResponse:
    try:
        row = common.sinks().get_sink(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkResponse(**row.model_dump())


@router.put("/sinks/{name}", response_model=ConfigApplyResponse, tags=["v1-sinks"])
async def upsert_sink(name: str, request: SinkUpsertRequest) -> ConfigApplyResponse:
    try:
        result = await common.sinks().upsert(name, request.model_dump())
    except ValueError as exc:
        raise APIError("VALIDATION_ERROR", str(exc), 422) from exc
    return ConfigApplyResponse(**result.model_dump())


@router.delete("/sinks/{name}", response_model=ConfigApplyResponse, tags=["v1-sinks"])
async def delete_sink(name: str) -> ConfigApplyResponse:
    try:
        result = await common.sinks().delete(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return ConfigApplyResponse(**result.model_dump())


@router.post("/sinks/{name}/verify", response_model=SinkTestResponse, tags=["v1-sinks"])
async def verify_sink(name: str) -> SinkTestResponse:
    try:
        result = await common.sinks().verify(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkTestResponse(**result.model_dump())


@router.post(
    "/sinks/{name}/write-test",
    response_model=SinkTestResponse,
    tags=["v1-sinks"],
)
async def sink_write_test(name: str) -> SinkTestResponse:
    try:
        result = await common.sinks().write_test(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkTestResponse(**result.model_dump())
