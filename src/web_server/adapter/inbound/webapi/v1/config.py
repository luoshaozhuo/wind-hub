"""Admin API v1 Config/Settings/Definitions/AdminState 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from web_server.adapter.inbound.webapi.context import get_ctx
from web_server.adapter.inbound.webapi.errors import APIError
from web_server.adapter.inbound.webapi.v1 import common
from web_server.adapter.inbound.webapi.v1.models import (
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
from web_server.application.app_context import AppContext
from web_server.application.config.admin_state import (
    AdminDefinitionsState,
    AdminDeviceItem,
    AdminSinkItem,
    AdminTaskItem,
)
from web_server.application.config.files import ConfigApplyResult
from web_server.infra.audit import audit

router = APIRouter()


def _raise_validation_failure(result: ConfigApplyResult) -> None:
    """统一配置写操作的失败语义：未写盘/未回滚的校验失败 → 422。

    写盘后 reload 失败且已回滚时保留 200 + success=false：live 状态
    未变更，但调用方必须看到事务未成功。
    """
    if not result.success and not result.rollback_performed:
        raise APIError(
            "VALIDATION_FAILED", "; ".join(result.errors) or "invalid config content", 422
        )


# ------------------------------ Config files


@router.get("/config/files", response_model=list[ConfigFileResponse], tags=["v1-config"])
async def list_config_files(ctx: AppContext = Depends(get_ctx)) -> list[ConfigFileResponse]:
    return [ConfigFileResponse(**row.model_dump()) for row in common.config_admin(ctx).list_files()]


@router.get("/config/files/{name}", response_model=ConfigContentResponse, tags=["v1-config"])
async def get_config_file(name: str, ctx: AppContext = Depends(get_ctx)) -> ConfigContentResponse:
    try:
        content = common.config_admin(ctx).read_file_redacted(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{name}'", 404) from None
    return ConfigContentResponse(name=name, content=content)


@router.post("/config/validate", response_model=ConfigReviewResponse, tags=["v1-config"])
async def validate_config(
    request: ConfigTextRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ConfigReviewResponse:
    try:
        review = common.config_admin(ctx).validate_file(request.name, request.content)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{request.name}'", 404) from None
    return ConfigReviewResponse(**review.model_dump())


@router.post("/config/apply", response_model=ConfigApplyResponse, tags=["v1-config"])
async def apply_config(
    request: ConfigTextRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ConfigApplyResponse:
    try:
        result = await common.config_admin(ctx).apply_file(
            request.name, request.content, source="config", comment=request.comment
        )
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{request.name}'", 404) from None
    audit("config.apply", file=request.name, comment=request.comment, success=result.success)
    _raise_validation_failure(result)
    return ConfigApplyResponse(**result.model_dump())


@router.post("/config/import", response_model=ConfigApplyResponse, tags=["v1-config"])
async def import_config(request: ConfigTextRequest) -> ConfigApplyResponse:
    """上传内容已由前端读取为文本时，与 Apply 共用完整校验/回滚链。"""
    return await apply_config(request)


@router.get("/config/backup", tags=["v1-config"])
async def download_config_backup(ctx: AppContext = Depends(get_ctx)) -> Response:
    payload = common.config_admin(ctx).backup_bytes()
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
async def config_history(ctx: AppContext = Depends(get_ctx)) -> list[ConfigRevisionResponse]:
    return [
        ConfigRevisionResponse(**row.model_dump()) for row in common.config_admin(ctx).history()
    ]


@router.post(
    "/config/history/{revision}/restore",
    response_model=ConfigApplyResponse,
    tags=["v1-config"],
)
async def restore_config(revision: int, ctx: AppContext = Depends(get_ctx)) -> ConfigApplyResponse:
    try:
        result = await common.config_admin(ctx).restore_revision(revision)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown revision '{revision}'", 404) from None
    audit("config.restore", revision=revision, success=result.success)
    return ConfigApplyResponse(**result.model_dump())


# ------------------------------ Settings


@router.get("/settings", response_model=SettingsResponse, tags=["v1-settings"])
async def get_settings(ctx: AppContext = Depends(get_ctx)) -> SettingsResponse:
    return SettingsResponse(**common.settings(ctx).get().model_dump())


@router.put("/settings", response_model=ConfigApplyResponse, tags=["v1-settings"])
async def update_settings(
    request: SettingsRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ConfigApplyResponse:
    from web_server.application.config.settings import SettingsUpdate

    result = await common.settings(ctx).update(SettingsUpdate(**request.model_dump()))
    audit("settings.update", success=result.success)
    _raise_validation_failure(result)
    return ConfigApplyResponse(**result.model_dump())


# ------------------------------ Definitions


@router.get("/definitions", response_model=DefinitionsResponse, tags=["v1-definitions"])
async def get_definitions(ctx: AppContext = Depends(get_ctx)) -> DefinitionsResponse:
    return DefinitionsResponse(**common.definitions(ctx).snapshot().model_dump())


@router.put(
    "/definitions/{kind}/{name}",
    response_model=ConfigApplyResponse,
    tags=["v1-definitions"],
)
async def upsert_definition(
    kind: str,
    name: str,
    request: DefinitionUpsertRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ConfigApplyResponse:
    try:
        result = await common.definitions(ctx).upsert(kind, name, request.value)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown definition kind '{kind}'", 404) from None
    audit("definition.upsert", kind=kind, name=name, success=result.success)
    _raise_validation_failure(result)
    return ConfigApplyResponse(**result.model_dump())


@router.delete(
    "/definitions/{kind}/{name}",
    response_model=ConfigApplyResponse,
    tags=["v1-definitions"],
)
async def delete_definition(
    kind: str,
    name: str,
    ctx: AppContext = Depends(get_ctx),
) -> ConfigApplyResponse:
    try:
        result = await common.definitions(ctx).delete(kind, name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown definition '{kind}/{name}'", 404) from None
    audit("definition.delete", kind=kind, name=name, success=result.success)
    _raise_validation_failure(result)
    return ConfigApplyResponse(**result.model_dump())


# ------------------------------ Structured config writes


@router.put(
    "/admin-state",
    response_model=ConfigApplyResponse,
    tags=["v1-admin-state"],
)
async def replace_admin_state(
    request: AdminStateRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ConfigApplyResponse:
    result = await common.admin_state(ctx).replace_all(
        devices=[AdminDeviceItem(**item.model_dump()) for item in request.devices],
        tasks=[AdminTaskItem(**item.model_dump()) for item in request.tasks],
        sinks=[AdminSinkItem(**item.model_dump()) for item in request.sinks],
        definitions=AdminDefinitionsState(**request.definitions.model_dump()),
    )
    audit(
        "admin_state.replace",
        devices=len(request.devices),
        tasks=len(request.tasks),
        sinks=len(request.sinks),
        success=result.success,
    )
    _raise_validation_failure(result)
    return ConfigApplyResponse(**result.model_dump())
