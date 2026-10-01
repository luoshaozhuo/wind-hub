"""Admin API v1 路由。

Phase 1 只提供 Overview、Devices、Tasks 和 Operation 查询；Device/Task 配置
CRUD、Verify、Config Apply 等后续接口不在本文件提前占位。
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import APIRouter, Query

from wind_hub.adapter.inbound.webapi.context import get_ctx
from wind_hub.adapter.inbound.webapi.errors import APIError
from wind_hub.adapter.inbound.webapi.v1.models import (
    DevicePageResponse,
    DeviceResponse,
    OperationResponse,
    OverviewResponse,
    PageMeta,
    TaskInstanceResponse,
    TaskPageResponse,
    TaskResponse,
)
from wind_hub.application.operation import OperationRecord
from wind_hub.application.usecase.device import DeviceSnapshot, DeviceUseCase
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
