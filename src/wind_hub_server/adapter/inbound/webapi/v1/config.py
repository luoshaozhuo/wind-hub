"""Admin API v1 Config/Settings/Definitions/AdminState 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Response

from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    AdminStateRequest,
    ConfigApplyResponse,
    ConfigContentResponse,
    ConfigFileResponse,
    ConfigReviewResponse,
    ConfigRevisionResponse,
    ConfigTextRequest,
    DefinitionsResponse,
    DefinitionUpsertRequest,
    SettingsRequest,
    SettingsResponse,
)
from wind_hub_server.application.config.admin_state import (
    AdminDefinitionsState,
    AdminDeviceItem,
    AdminSinkItem,
    AdminTaskItem,
)

router = APIRouter()


# ------------------------------ Config files


@router.get("/config/files", response_model=list[ConfigFileResponse], tags=["v1-config"])
async def list_config_files() -> list[ConfigFileResponse]:
    return [
        ConfigFileResponse(**row.model_dump())
        for row in common.config_admin().list_files()
    ]


@router.get("/config/files/{name}", response_model=ConfigContentResponse, tags=["v1-config"])
async def get_config_file(name: str) -> ConfigContentResponse:
    try:
        content = common.config_admin().read_file(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{name}'", 404) from None
    return ConfigContentResponse(name=name, content=content)


@router.post("/config/validate", response_model=ConfigReviewResponse, tags=["v1-config"])
async def validate_config(request: ConfigTextRequest) -> ConfigReviewResponse:
    try:
        review = common.config_admin().validate_file(request.name, request.content)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{request.name}'", 404) from None
    return ConfigReviewResponse(**review.model_dump())


@router.post("/config/apply", response_model=ConfigApplyResponse, tags=["v1-config"])
async def apply_config(request: ConfigTextRequest) -> ConfigApplyResponse:
    try:
        result = await common.config_admin().apply_file(
            request.name, request.content, source="config", comment=request.comment
        )
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{request.name}'", 404) from None
    return ConfigApplyResponse(**result.model_dump())


@router.post("/config/import", response_model=ConfigApplyResponse, tags=["v1-config"])
async def import_config(request: ConfigTextRequest) -> ConfigApplyResponse:
    """上传内容已由前端读取为文本时，与 Apply 共用完整校验/回滚链。"""
    return await apply_config(request)


@router.get("/config/backup", tags=["v1-config"])
async def download_config_backup() -> Response:
    payload = common.config_admin().backup_bytes()
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=wind-hub-config.zip"},
    )


@router.get(
    "/config/history",
    response_model=list[ConfigRevisionResponse],
    tags=["v1-config"],
)
async def config_history() -> list[ConfigRevisionResponse]:
    return [
        ConfigRevisionResponse(**row.model_dump())
        for row in common.config_admin().history()
    ]


@router.post(
    "/config/history/{revision}/restore",
    response_model=ConfigApplyResponse,
    tags=["v1-config"],
)
async def restore_config(revision: int) -> ConfigApplyResponse:
    try:
        result = await common.config_admin().restore_revision(revision)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown revision '{revision}'", 404) from None
    return ConfigApplyResponse(**result.model_dump())


# ------------------------------ Settings


@router.get("/settings", response_model=SettingsResponse, tags=["v1-settings"])
async def get_settings() -> SettingsResponse:
    return SettingsResponse(**common.settings().get().model_dump())


@router.put("/settings", response_model=ConfigApplyResponse, tags=["v1-settings"])
async def update_settings(request: SettingsRequest) -> ConfigApplyResponse:
    from wind_hub_server.application.config.settings import SettingsUpdate

    result = await common.settings().update(SettingsUpdate(**request.model_dump()))
    return ConfigApplyResponse(**result.model_dump())


# ------------------------------ Definitions


@router.get("/definitions", response_model=DefinitionsResponse, tags=["v1-definitions"])
async def get_definitions() -> DefinitionsResponse:
    return DefinitionsResponse(**common.definitions().snapshot().model_dump())


@router.put(
    "/definitions/{kind}/{name}",
    response_model=ConfigApplyResponse,
    tags=["v1-definitions"],
)
async def upsert_definition(
    kind: str, name: str, request: DefinitionUpsertRequest
) -> ConfigApplyResponse:
    try:
        result = await common.definitions().upsert(kind, name, request.value)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown definition kind '{kind}'", 404) from None
    return ConfigApplyResponse(**result.model_dump())


@router.delete(
    "/definitions/{kind}/{name}",
    response_model=ConfigApplyResponse,
    tags=["v1-definitions"],
)
async def delete_definition(kind: str, name: str) -> ConfigApplyResponse:
    try:
        result = await common.definitions().delete(kind, name)
    except KeyError:
        raise APIError(
            "NOT_FOUND", f"unknown definition '{kind}/{name}'", 404
        ) from None
    return ConfigApplyResponse(**result.model_dump())


# ------------------------------ Structured config writes


@router.put(
    "/admin-state",
    response_model=ConfigApplyResponse,
    tags=["v1-admin-state"],
)
async def replace_admin_state(request: AdminStateRequest) -> ConfigApplyResponse:
    result = await common.admin_state().replace_all(
        devices=[AdminDeviceItem(**item.model_dump()) for item in request.devices],
        tasks=[AdminTaskItem(**item.model_dump()) for item in request.tasks],
        sinks=[AdminSinkItem(**item.model_dump()) for item in request.sinks],
        definitions=AdminDefinitionsState(**request.definitions.model_dump()),
    )
    return ConfigApplyResponse(**result.model_dump())
