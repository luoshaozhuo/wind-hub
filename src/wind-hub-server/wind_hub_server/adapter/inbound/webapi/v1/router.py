"""Admin API v1 路由。

Admin API v1 当前覆盖 Phase 1–5：运行总览、设备/任务、Data/Trend/Command、
Config/Settings/Definitions、Sinks/Diagnostics、Quality/Logs/System Health。
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import APIRouter, Query, Response

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    DeviceCommandRequest,
    DeviceCommandResponse,
    DeviceDataItemResponse,
    DeviceDataPageResponse,
    DevicePageResponse,
    DeviceResponse,
    AdminDefinitionsRequest,
    AdminDevicesRequest,
    AdminSinksRequest,
    AdminTasksRequest,
    AdminStateRequest,
    ConfigApplyResponse,
    ConfigContentResponse,
    ConfigFileResponse,
    ConfigReviewResponse,
    ConfigRevisionResponse,
    ConfigTextRequest,
    DefinitionsResponse,
    DefinitionUpsertRequest,
    LogEntryResponse,
    LogPageResponse,
    PingRequest,
    PingResponse,
    PortProbeResponse,
    PortsRequest,
    PointTableTestRequest,
    QualityResponse,
    ProtocolCheckRequest,
    ProtocolCheckResponse,
    ProtocolReadRequest,
    ProtocolReadResponse,
    ProtocolWriteRequest,
    SettingsRequest,
    SettingsResponse,
    SystemHealthResponse,
    SinkResponse,
    SinkTestResponse,
    SinkUpsertRequest,
    SubnetScanRequest,
    OperationResponse,
    OverviewResponse,
    PageMeta,
    TaskInstanceResponse,
    TaskPageResponse,
    TaskResponse,
    TrendSampleResponse,
    TrendSeriesResponse,
)
from wind_hub_server.application.operation import OperationRecord
from wind_hub_server.application.usecase.admin_state import (
    AdminDefinitionsState,
    AdminDeviceItem,
    AdminSinkItem,
    AdminStateUseCase,
    AdminTaskItem,
)
from wind_hub_server.application.usecase.config_admin import ConfigAdminUseCase
from wind_hub_server.application.usecase.definitions import DefinitionsUseCase
from wind_hub_server.application.usecase.device import DeviceSnapshot, DeviceUseCase
from wind_hub_server.application.usecase.device_control import DeviceControlUseCase
from wind_hub_server.application.usecase.device_data import DeviceDataUseCase, TrendSeries
from wind_hub_server.application.usecase.overview import OverviewSnapshot, OverviewUseCase
from wind_hub_server.application.usecase.logs import LogsUseCase
from wind_hub_server.application.usecase.quality import QualityUseCase, QualityWindow
from wind_hub_server.application.usecase.diagnostic import DiagnosticUseCase
from wind_hub_server.application.usecase.settings import SettingsUseCase
from wind_hub_server.application.usecase.sink import SinkUseCase
from wind_hub_server.application.usecase.system_health import HealthRange, SystemHealthUseCase
from wind_hub.application.usecase.task import (
    TaskInstanceDetail,
    TaskSummary,
    TaskUseCase,
)

T = TypeVar("T")

router = APIRouter(prefix="/api/v1")


def _devices() -> DeviceUseCase:
    """返回 V1 DeviceUseCase；未装配时按服务不可用处理。"""
    ctx = get_ctx()
    if ctx.devices is None:
        raise APIError("SERVICE_UNAVAILABLE", "device use case is not configured", 503)
    return ctx.devices


def _device_data() -> DeviceDataUseCase:
    """返回 Devices Data/Trend 用例。"""
    ctx = get_ctx()
    if ctx.device_data is None:
        raise APIError("SERVICE_UNAVAILABLE", "device data use case is not configured", 503)
    return ctx.device_data


def _device_control() -> DeviceControlUseCase:
    """返回设备控制与回读用例。"""
    ctx = get_ctx()
    if ctx.device_control is None:
        raise APIError("SERVICE_UNAVAILABLE", "device control use case is not configured", 503)
    return ctx.device_control


def _admin_state() -> AdminStateUseCase:
    ctx = get_ctx()
    if ctx.admin_state is None:
        raise APIError("SERVICE_UNAVAILABLE", "admin state is not configured", 503)
    return ctx.admin_state


def _config_admin() -> ConfigAdminUseCase:
    ctx = get_ctx()
    if ctx.config_admin is None:
        raise APIError("SERVICE_UNAVAILABLE", "config admin is not configured", 503)
    return ctx.config_admin


def _settings() -> SettingsUseCase:
    ctx = get_ctx()
    if ctx.settings is None:
        raise APIError("SERVICE_UNAVAILABLE", "settings use case is not configured", 503)
    return ctx.settings


def _definitions() -> DefinitionsUseCase:
    ctx = get_ctx()
    if ctx.definitions is None:
        raise APIError("SERVICE_UNAVAILABLE", "definitions use case is not configured", 503)
    return ctx.definitions


def _sinks() -> SinkUseCase:
    ctx = get_ctx()
    if ctx.sinks is None:
        raise APIError("SERVICE_UNAVAILABLE", "sink use case is not configured", 503)
    return ctx.sinks


def _diagnostics() -> DiagnosticUseCase:
    ctx = get_ctx()
    if ctx.diagnostics is None:
        raise APIError("SERVICE_UNAVAILABLE", "diagnostics use case is not configured", 503)
    return ctx.diagnostics


def _quality() -> QualityUseCase:
    ctx = get_ctx()
    if ctx.quality is None:
        raise APIError("SERVICE_UNAVAILABLE", "quality use case is not configured", 503)
    return ctx.quality


def _logs() -> LogsUseCase:
    ctx = get_ctx()
    if ctx.logs is None:
        raise APIError("SERVICE_UNAVAILABLE", "logs use case is not configured", 503)
    return ctx.logs


def _system_health() -> SystemHealthUseCase:
    ctx = get_ctx()
    if ctx.system_health is None:
        raise APIError("SERVICE_UNAVAILABLE", "system health use case is not configured", 503)
    return ctx.system_health


def _tasks() -> TaskUseCase:
    """返回 TaskUseCase；未装配时按服务不可用处理。"""
    ctx = get_ctx()
    if ctx.tasks is None:
        raise APIError("SERVICE_UNAVAILABLE", "tasks use case is not configured", 503)
    return ctx.tasks


def _overview() -> OverviewUseCase:
    """返回 OverviewUseCase；未装配时按服务不可用处理。"""
    ctx = get_ctx()
    if ctx.overview is None:
        raise APIError("SERVICE_UNAVAILABLE", "overview use case is not configured", 503)
    return ctx.overview


def _page(items: list[T], page: int, page_size: int) -> tuple[list[T], PageMeta]:
    """对已完成过滤的内存快照执行稳定分页。"""
    total = len(items)
    start = (page - 1) * page_size
    return items[start : start + page_size], PageMeta(
        page=page, page_size=page_size, total=total
    )


def _device_response(row: DeviceSnapshot) -> DeviceResponse:
    """Application DeviceSnapshot 转 API DTO。"""
    return DeviceResponse(**row.model_dump())


def _task_response(row: TaskSummary) -> TaskResponse:
    """Application TaskSummary 转 API DTO。"""
    return TaskResponse(**row.model_dump())


def _instance_response(row: TaskInstanceDetail) -> TaskInstanceResponse:
    """Application TaskInstanceDetail 转 API DTO。"""
    data = row.model_dump()
    data["state"] = row.state.value
    return TaskInstanceResponse(**data)


def _operation_response(row: OperationRecord) -> OperationResponse:
    """Application OperationRecord 转 API DTO。"""
    data = row.model_dump()
    data["state"] = row.state.value
    return OperationResponse(**data)


@router.get("/overview", response_model=OverviewResponse, tags=["v1-overview"])
async def get_overview() -> OverviewResponse:
    """返回 wind-hub-admin 总览页的一次聚合运行快照。"""
    snapshot: OverviewSnapshot = await _overview().snapshot()
    return OverviewResponse(**snapshot.model_dump())


@router.get("/devices", response_model=DevicePageResponse, tags=["v1-devices"])
async def list_devices(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None),
) -> DevicePageResponse:
    """分页查询设备；搜索作用于完整结果集后再分页。"""
    rows = await _devices().list_devices(search)
    paged, meta = _page(rows, page, page_size)
    return DevicePageResponse(
        items=[_device_response(row) for row in paged],
        page=meta,
    )


@router.get("/devices/{device_id}", response_model=DeviceResponse, tags=["v1-devices"])
async def get_device(device_id: str) -> DeviceResponse:
    """查询单设备静态配置与实时连接状态。"""
    try:
        row = await _devices().get_device(device_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown device '{device_id}'", 404) from None
    return _device_response(row)


@router.get("/tasks", response_model=TaskPageResponse, tags=["v1-tasks"])
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None),
) -> TaskPageResponse:
    """分页查询 Task Definition 与实例聚合运行状态。"""
    rows = await _tasks().list_task_summaries()
    query = (search or "").strip().lower()
    if query:
        rows = [
            row
            for row in rows
            if any(
                query in str(value or "").lower()
                for value in (row.task_id, row.device, row.device_group, row.point_group)
            )
        ]
    rows = sorted(rows, key=lambda row: row.task_id)
    paged, meta = _page(rows, page, page_size)
    return TaskPageResponse(
        items=[_task_response(row) for row in paged],
        page=meta,
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse, tags=["v1-tasks"])
async def get_task(task_id: str) -> TaskResponse:
    """查询单个 Task 的聚合运行状态。"""
    try:
        row = await _tasks().get_task_summary(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    return _task_response(row)


@router.get(
    "/tasks/{task_id}/instances",
    response_model=list[TaskInstanceResponse],
    tags=["v1-tasks"],
)
async def list_task_instances(task_id: str) -> list[TaskInstanceResponse]:
    """查询指定 Task 展开的实例。"""
    try:
        rows = await _tasks().list_task_instances(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    return [_instance_response(row) for row in rows]


@router.post("/tasks/{task_id}/start", response_model=TaskResponse, tags=["v1-tasks"])
async def start_task(task_id: str) -> TaskResponse:
    """启动 Task 的全部实例；禁用 Task 返回 409。"""
    try:
        row = await _tasks().start_task(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    except ValueError as exc:
        raise APIError("TASK_DISABLED", str(exc), 409) from exc
    return _task_response(row)


@router.post("/tasks/{task_id}/stop", response_model=TaskResponse, tags=["v1-tasks"])
async def stop_task(task_id: str) -> TaskResponse:
    """停止 Task 的全部实例；不停止 Runtime 或设备连接。"""
    try:
        row = await _tasks().stop_task(task_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown task '{task_id}'", 404) from None
    return _task_response(row)


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
    return _operation_response(row)


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
    """从 LatestPointStore 查询设备当前点值，不主动访问 PLC。"""
    try:
        rows = await _device_data().list_data(
            device_id, search=search, point_group=point_group
        )
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown device '{device_id}'", 404) from None
    paged, meta = _page(rows, page, page_size)
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
    """查询短期内存趋势，不访问历史数据库。"""
    try:
        series = await _device_data().trend(
            device_id,
            point_id,
            window_seconds=window_seconds,
            limit_per_point=limit_per_point,
        )
    except KeyError as exc:
        raise APIError("NOT_FOUND", str(exc), 404) from exc
    return [_trend_response(row) for row in series]


@router.post(
    "/devices/{device_id}/commands",
    response_model=DeviceCommandResponse,
    tags=["v1-devices"],
)
async def send_device_command(
    device_id: str, request: DeviceCommandRequest
) -> DeviceCommandResponse:
    """真实写入设备；写成功后回读并刷新 Latest/Trend Store。"""
    result = await _device_control().send(
        device_id,
        request.point_id,
        request.value,
        timeout=request.timeout,
        command_id=request.command_id,
    )
    return DeviceCommandResponse(**result.model_dump())


def _trend_response(row: TrendSeries) -> TrendSeriesResponse:
    """Application TrendSeries 转 API DTO。"""
    return TrendSeriesResponse(
        point_id=row.point_id,
        variable_name=row.variable_name,
        unit=row.unit,
        unit_symbol=row.unit_symbol,
        samples=[
            TrendSampleResponse(
                value=sample.value,
                quality=sample.quality.value,
                timestamp=sample.timestamp,
                source=sample.source,
            )
            for sample in row.samples
        ],
    )


# ------------------------------ Phase 3: Config / Settings / Definitions

@router.get("/config/files", response_model=list[ConfigFileResponse], tags=["v1-config"])
async def list_config_files() -> list[ConfigFileResponse]:
    return [ConfigFileResponse(**row.model_dump()) for row in _config_admin().list_files()]


@router.get("/config/files/{name}", response_model=ConfigContentResponse, tags=["v1-config"])
async def get_config_file(name: str) -> ConfigContentResponse:
    try:
        content = _config_admin().read_file(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{name}'", 404) from None
    return ConfigContentResponse(name=name, content=content)


@router.post("/config/validate", response_model=ConfigReviewResponse, tags=["v1-config"])
async def validate_config(request: ConfigTextRequest) -> ConfigReviewResponse:
    try:
        review = _config_admin().validate_file(request.name, request.content)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown config file '{request.name}'", 404) from None
    return ConfigReviewResponse(**review.model_dump())


@router.post("/config/review", response_model=ConfigReviewResponse, tags=["v1-config"])
async def review_config(request: ConfigTextRequest) -> ConfigReviewResponse:
    return await validate_config(request)


@router.post("/config/apply", response_model=ConfigApplyResponse, tags=["v1-config"])
async def apply_config(request: ConfigTextRequest) -> ConfigApplyResponse:
    try:
        result = await _config_admin().apply_file(
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
    payload = _config_admin().backup_bytes()
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
        ConfigRevisionResponse(**row.model_dump()) for row in _config_admin().history()
    ]


@router.post(
    "/config/history/{revision}/restore",
    response_model=ConfigApplyResponse,
    tags=["v1-config"],
)
async def restore_config(revision: int) -> ConfigApplyResponse:
    try:
        result = await _config_admin().restore_revision(revision)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown revision '{revision}'", 404) from None
    return ConfigApplyResponse(**result.model_dump())


@router.get("/settings", response_model=SettingsResponse, tags=["v1-settings"])
async def get_settings() -> SettingsResponse:
    return SettingsResponse(**_settings().get().model_dump())


@router.put("/settings", response_model=ConfigApplyResponse, tags=["v1-settings"])
async def update_settings(request: SettingsRequest) -> ConfigApplyResponse:
    from wind_hub_server.application.usecase.settings import SettingsUpdate

    result = await _settings().update(SettingsUpdate(**request.model_dump()))
    return ConfigApplyResponse(**result.model_dump())


@router.get("/definitions", response_model=DefinitionsResponse, tags=["v1-definitions"])
async def get_definitions() -> DefinitionsResponse:
    return DefinitionsResponse(**_definitions().snapshot().model_dump())


@router.put(
    "/definitions/{kind}/{name}",
    response_model=ConfigApplyResponse,
    tags=["v1-definitions"],
)
async def upsert_definition(
    kind: str, name: str, request: DefinitionUpsertRequest
) -> ConfigApplyResponse:
    try:
        result = await _definitions().upsert(kind, name, request.value)
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
        result = await _definitions().delete(kind, name)
    except KeyError:
        raise APIError(
            "NOT_FOUND", f"unknown definition '{kind}/{name}'", 404
        ) from None
    return ConfigApplyResponse(**result.model_dump())


# ------------------------------ Phase 4: Sinks / Diagnostics

@router.get("/sinks", response_model=list[SinkResponse], tags=["v1-sinks"])
async def list_sinks() -> list[SinkResponse]:
    return [SinkResponse(**row.model_dump()) for row in _sinks().list_sinks()]


@router.get("/sinks/{name}", response_model=SinkResponse, tags=["v1-sinks"])
async def get_sink(name: str) -> SinkResponse:
    try:
        row = _sinks().get_sink(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkResponse(**row.model_dump())


@router.put("/sinks/{name}", response_model=ConfigApplyResponse, tags=["v1-sinks"])
async def upsert_sink(name: str, request: SinkUpsertRequest) -> ConfigApplyResponse:
    try:
        result = await _sinks().upsert(name, request.model_dump())
    except ValueError as exc:
        raise APIError("VALIDATION_ERROR", str(exc), 422) from exc
    return ConfigApplyResponse(**result.model_dump())


@router.delete("/sinks/{name}", response_model=ConfigApplyResponse, tags=["v1-sinks"])
async def delete_sink(name: str) -> ConfigApplyResponse:
    try:
        result = await _sinks().delete(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return ConfigApplyResponse(**result.model_dump())


@router.post("/sinks/{name}/verify", response_model=SinkTestResponse, tags=["v1-sinks"])
async def verify_sink(name: str) -> SinkTestResponse:
    try:
        result = await _sinks().verify(name)
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
        result = await _sinks().write_test(name)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown sink '{name}'", 404) from None
    return SinkTestResponse(**result.model_dump())


@router.post("/diagnostics/ping", response_model=PingResponse, tags=["v1-diagnostics"])
async def diagnostic_ping(request: PingRequest) -> PingResponse:
    result = await _diagnostics().ping(request.host, request.timeout)
    return PingResponse(**result.model_dump())


@router.post(
    "/diagnostics/ports",
    response_model=list[PortProbeResponse],
    tags=["v1-diagnostics"],
)
async def diagnostic_ports(request: PortsRequest) -> list[PortProbeResponse]:
    rows = await _diagnostics().ports(request.host, request.ports, request.timeout)
    return [PortProbeResponse(**row.model_dump()) for row in rows]


@router.post("/diagnostics/subnet-scan", response_model=OperationResponse, tags=["v1-diagnostics"])
async def diagnostic_subnet_scan(request: SubnetScanRequest) -> OperationResponse:
    try:
        operation = _diagnostics().start_subnet_scan(
            request.network, timeout=request.timeout, ports=request.ports
        )
    except ValueError as exc:
        raise APIError("VALIDATION_ERROR", str(exc), 422) from exc
    return _operation_response(operation)


@router.post(
    "/diagnostics/protocol/check",
    response_model=ProtocolCheckResponse,
    tags=["v1-diagnostics"],
)
async def diagnostic_protocol_check(
    request: ProtocolCheckRequest,
) -> ProtocolCheckResponse:
    try:
        connected = await _diagnostics().protocol_check(request.device_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown device '{request.device_id}'", 404) from None
    return ProtocolCheckResponse(device_id=request.device_id, connected=connected)


@router.post(
    "/diagnostics/protocol/read",
    response_model=ProtocolReadResponse,
    tags=["v1-diagnostics"],
)
async def diagnostic_protocol_read(request: ProtocolReadRequest) -> ProtocolReadResponse:
    try:
        value = await _diagnostics().read(request.device_id, request.point_id)
    except Exception as exc:
        raise APIError("PROTOCOL_READ_FAILED", str(exc), 503) from exc
    return ProtocolReadResponse(
        device_id=value.device_id,
        point_id=value.point_id,
        value=value.value,
        quality=value.quality.value,
        timestamp=value.timestamp,
        source=value.source,
    )


@router.post(
    "/diagnostics/point-table",
    response_model=OperationResponse,
    tags=["v1-diagnostics"],
)
async def diagnostic_point_table(request: PointTableTestRequest) -> OperationResponse:
    try:
        operation = _diagnostics().start_point_table_test(request.device_id)
    except KeyError:
        raise APIError(
            "NOT_FOUND", f"unknown device '{request.device_id}'", 404
        ) from None
    return _operation_response(operation)


@router.post(
    "/diagnostics/protocol/write",
    response_model=DeviceCommandResponse,
    tags=["v1-diagnostics"],
)
async def diagnostic_protocol_write(
    request: ProtocolWriteRequest,
) -> DeviceCommandResponse:
    result = await _diagnostics().write(request.device_id, request.point_id, request.value)
    return DeviceCommandResponse(**result.model_dump())


# ------------------------------ Phase 5: Quality / Logs / System Health

@router.get("/quality", response_model=QualityResponse, tags=["v1-quality"])
async def get_quality(
    window: QualityWindow = Query("24h"),
) -> QualityResponse:
    snapshot = _quality().snapshot(window)
    return QualityResponse(**snapshot.model_dump())


@router.post("/quality/check", response_model=QualityResponse, tags=["v1-quality"])
async def run_quality_check(
    window: QualityWindow = Query("24h"),
) -> QualityResponse:
    """立即采样并按真实窗口重算。"""
    snapshot = _quality().snapshot(window)
    return QualityResponse(**snapshot.model_dump())


@router.get("/logs", response_model=LogPageResponse, tags=["v1-logs"])
async def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    level: str | None = Query(None),
    source: str | None = Query(None),
    keyword: str | None = Query(None),
) -> LogPageResponse:
    result = _logs().list_logs(
        page=page, page_size=page_size, level=level, source=source, keyword=keyword
    )
    return LogPageResponse(
        items=[LogEntryResponse(**row.model_dump()) for row in result.items],
        page=PageMeta(page=result.page, page_size=result.page_size, total=result.total),
    )


@router.get("/logs/sources", response_model=list[str], tags=["v1-logs"])
async def list_log_sources() -> list[str]:
    return _logs().sources()


@router.get(
    "/system-health",
    response_model=SystemHealthResponse,
    tags=["v1-system-health"],
)
async def get_system_health(
    range_name: HealthRange = Query("24h", alias="range"),
) -> SystemHealthResponse:
    snapshot = _system_health().snapshot(range_name)
    return SystemHealthResponse(**snapshot.model_dump())


# ------------------------------ Final integration: structured config writes

@router.put(
    "/admin-state/devices",
    response_model=ConfigApplyResponse,
    tags=["v1-admin-state"],
)
async def replace_admin_devices(
    request: AdminDevicesRequest,
) -> ConfigApplyResponse:
    result = await _admin_state().replace_devices(
        [AdminDeviceItem(**item.model_dump()) for item in request.items]
    )
    return ConfigApplyResponse(**result.model_dump())


@router.put(
    "/admin-state/tasks",
    response_model=ConfigApplyResponse,
    tags=["v1-admin-state"],
)
async def replace_admin_tasks(request: AdminTasksRequest) -> ConfigApplyResponse:
    result = await _admin_state().replace_tasks(
        [AdminTaskItem(**item.model_dump()) for item in request.items]
    )
    return ConfigApplyResponse(**result.model_dump())


@router.put(
    "/admin-state/sinks",
    response_model=ConfigApplyResponse,
    tags=["v1-admin-state"],
)
async def replace_admin_sinks(request: AdminSinksRequest) -> ConfigApplyResponse:
    result = await _admin_state().replace_sinks(
        [AdminSinkItem(**item.model_dump()) for item in request.items]
    )
    return ConfigApplyResponse(**result.model_dump())


@router.put(
    "/admin-state/definitions",
    response_model=ConfigApplyResponse,
    tags=["v1-admin-state"],
)
async def replace_admin_definitions(
    request: AdminDefinitionsRequest,
) -> ConfigApplyResponse:
    result = await _admin_state().replace_definitions(
        AdminDefinitionsState(**request.model_dump())
    )
    return ConfigApplyResponse(**result.model_dump())


@router.put(
    "/admin-state",
    response_model=ConfigApplyResponse,
    tags=["v1-admin-state"],
)
async def replace_admin_state(request: AdminStateRequest) -> ConfigApplyResponse:
    result = await _admin_state().replace_all(
        devices=[AdminDeviceItem(**item.model_dump()) for item in request.devices],
        tasks=[AdminTaskItem(**item.model_dump()) for item in request.tasks],
        sinks=[AdminSinkItem(**item.model_dump()) for item in request.sinks],
        definitions=AdminDefinitionsState(**request.definitions.model_dump()),
    )
    return ConfigApplyResponse(**result.model_dump())
