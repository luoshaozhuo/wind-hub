"""Admin API v1 路由。

Phase 1/2 提供 Overview、Devices、Tasks、Operation、Data/Trend 和 Command；
Device/Task 配置 CRUD、Verify、Config Apply 等后续接口不在本文件提前占位。
"""

from __future__ import annotations

from typing import Any, TypeVar

from fastapi import APIRouter, Query, Response

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.v1.models import (
    ConfigTextRequest,
    DiagnosticPingRequest,
    DiagnosticPointRequest,
    DiagnosticTcpRequest,
    DeviceCommandRequest,
    DeviceCommandResponse,
    DeviceDataItemResponse,
    DeviceDataPageResponse,
    DevicePageResponse,
    DeviceResponse,
    OperationResponse,
    SettingsUpdateRequest,
    SubnetScanRequest,
    OverviewResponse,
    PageMeta,
    TaskInstanceResponse,
    TaskPageResponse,
    TaskResponse,
    TrendSampleResponse,
    TrendSeriesResponse,
)
from wind_hub.application.operation import OperationRecord
from wind_hub.application.usecase.device import DeviceSnapshot, DeviceUseCase
from wind_hub.application.usecase.device_control import DeviceControlUseCase
from wind_hub.application.usecase.device_data import DeviceDataUseCase, TrendSeries
from wind_hub.application.usecase.overview import OverviewSnapshot, OverviewUseCase
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


# ---------------------------------------------------------------------------
# Phase 3-5 Admin endpoints
# ---------------------------------------------------------------------------


def _required(name: str) -> Any:
    """从 AppContext 取已装配服务；缺失统一返回 503。"""
    ctx = get_ctx()
    value = getattr(ctx, name)
    if value is None:
        raise APIError(
            "SERVICE_UNAVAILABLE",
            f"{name} is not configured",
            503,
        )
    return value


@router.get("/config/files", tags=["v1-config"])
async def config_files() -> list[dict[str, Any]]:
    return _required("admin_config").list_files()


@router.get("/config/files/{name}", tags=["v1-config"])
async def config_file(name: str) -> dict[str, str]:
    try:
        text = _required("admin_config").read_file(name)
    except KeyError:
        raise APIError(
            "NOT_FOUND",
            f"unknown config file '{name}'",
            404,
        ) from None
    return {"name": name, "text": text}


@router.post("/config/validate", tags=["v1-config"])
async def config_validate(
    request: ConfigTextRequest,
) -> dict[str, Any]:
    return _required("admin_config").validate(
        request.file,
        request.text,
    )


@router.post("/config/apply", tags=["v1-config"])
async def config_apply(
    request: ConfigTextRequest,
) -> dict[str, Any]:
    return await _required("admin_config").apply(
        request.file,
        request.text,
    )


@router.get("/config/history", tags=["v1-config"])
async def config_history() -> list[dict[str, Any]]:
    return _required("admin_config").history()


@router.post(
    "/config/history/{revision}/restore",
    tags=["v1-config"],
)
async def config_restore(
    revision: int,
) -> dict[str, Any]:
    try:
        return await _required("admin_config").restore(
            revision
        )
    except KeyError:
        raise APIError(
            "NOT_FOUND",
            f"unknown revision '{revision}'",
            404,
        ) from None


@router.get("/config/backup", tags=["v1-config"])
async def config_backup() -> Response:
    data = _required("admin_config").backup_zip()
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition":
                'attachment; filename="wind-hub-config.zip"'
        },
    )


@router.get("/settings", tags=["v1-config"])
async def get_settings() -> dict[str, Any]:
    return _required("admin_config").settings()


@router.put("/settings", tags=["v1-config"])
async def put_settings(
    request: SettingsUpdateRequest,
) -> dict[str, Any]:
    payload = request.model_dump(exclude_none=True)
    return await _required(
        "admin_config"
    ).update_settings(payload)


@router.get("/definitions", tags=["v1-config"])
async def definitions() -> dict[str, Any]:
    return _required("admin_config").definitions()


@router.get("/sinks", tags=["v1-sinks"])
async def sinks() -> list[dict[str, Any]]:
    return _required("sinks").list()


@router.post(
    "/sinks/{name}/verify",
    tags=["v1-sinks"],
)
async def verify_sink(name: str) -> dict[str, Any]:
    try:
        return await _required("sinks").verify(name)
    except KeyError:
        raise APIError(
            "NOT_FOUND",
            f"unknown sink '{name}'",
            404,
        ) from None


@router.post(
    "/sinks/{name}/write-test",
    tags=["v1-sinks"],
)
async def write_test_sink(
    name: str,
) -> dict[str, Any]:
    try:
        return await _required("sinks").write_test(
            name
        )
    except KeyError:
        raise APIError(
            "NOT_FOUND",
            f"unknown sink '{name}'",
            404,
        ) from None


@router.post(
    "/diagnostics/ping",
    tags=["v1-diagnostics"],
)
async def diagnostic_ping(
    request: DiagnosticPingRequest,
) -> dict[str, str]:
    return await _required("diagnostics").ping(
        request.host
    )


@router.post(
    "/diagnostics/tcp",
    tags=["v1-diagnostics"],
)
async def diagnostic_tcp(
    request: DiagnosticTcpRequest,
) -> list[dict[str, str | int]]:
    return await _required("diagnostics").tcp(
        request.host,
        request.ports,
    )


@router.post(
    "/diagnostics/protocol/read",
    tags=["v1-diagnostics"],
)
async def diagnostic_read(
    request: DiagnosticPointRequest,
) -> dict[str, Any]:
    return await _required(
        "diagnostics"
    ).protocol_read(
        request.device_id,
        request.point_id,
    )


@router.post(
    "/diagnostics/protocol/write",
    tags=["v1-diagnostics"],
)
async def diagnostic_write(
    request: DiagnosticPointRequest,
) -> dict[str, Any]:
    return await _required(
        "diagnostics"
    ).protocol_write(
        request.device_id,
        request.point_id,
        request.value,
    )


@router.post(
    "/diagnostics/subnet-scan",
    tags=["v1-diagnostics"],
)
async def subnet_scan(
    request: SubnetScanRequest,
) -> dict[str, str]:
    try:
        operation_id = await _required(
            "diagnostics"
        ).start_scan(
            request.cidr,
            request.ports,
        )
    except ValueError as exc:
        raise APIError(
            "VALIDATION_ERROR",
            str(exc),
            400,
        ) from exc
    return {"operation_id": operation_id}


@router.post(
    "/verify/devices/{device_id}",
    tags=["v1-devices"],
)
async def verify_device(
    device_id: str,
) -> dict[str, Any]:
    try:
        return await _required(
            "device_verify"
        ).verify(device_id)
    except KeyError:
        raise APIError(
            "NOT_FOUND",
            f"unknown device '{device_id}'",
            404,
        ) from None


@router.post(
    "/verify/devices",
    tags=["v1-devices"],
)
async def verify_devices(
    device_ids: list[str],
) -> dict[str, Any]:
    return await _required(
        "device_verify"
    ).verify_all(device_ids)


@router.get("/quality", tags=["v1-quality"])
async def quality(
    window: str = "24 h",
) -> dict[str, Any]:
    return _required("quality").snapshot(window)


@router.post("/quality/check", tags=["v1-quality"])
async def quality_check(
    window: str = "24 h",
) -> dict[str, Any]:
    return _required("quality").snapshot(window)


@router.get("/system/health", tags=["v1-system"])
async def system_health(
    window: str = "24 h",
) -> dict[str, Any]:
    return _required(
        "system_health"
    ).snapshot(window)


@router.get("/logs", tags=["v1-logs"])
async def logs(
    level: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    rows = _required("logs").query(
        level=level,
        source=source,
        keyword=keyword,
    )
    start = (page - 1) * page_size
    return {
        "items": [
            row.model_dump(mode="json")
            for row in rows[
                start : start + page_size
            ]
        ],
        "page": {
            "page": page,
            "page_size": page_size,
            "total": len(rows),
        },
    }
