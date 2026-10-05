"""Admin API v1 共享装配与 Application → API DTO 转换辅助。"""

from __future__ import annotations

from typing import TypeVar

from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1.models import (
    DeviceResponse,
    OperationResponse,
    PageMeta,
    TaskInstanceResponse,
    TaskResponse,
    TrendSampleResponse,
    TrendSeriesResponse,
)
from wind_hub_server.application.app_context import AppContext
from wind_hub_server.application.config.admin_state import AdminStateService
from wind_hub_server.application.config.definitions import DefinitionQueryService
from wind_hub_server.application.config.files import ConfigFileService
from wind_hub_server.application.config.settings import SettingsService
from wind_hub_server.application.device.command import DeviceCommandService
from wind_hub_server.application.device.data import DeviceDataService, TrendSeries
from wind_hub_server.application.device.diagnostic import DiagnosticService
from wind_hub_server.application.device.query import DeviceQueryService, DeviceSnapshot
from wind_hub_server.application.monitoring.health import SystemHealthService
from wind_hub_server.application.monitoring.logs import LogQueryService
from wind_hub_server.application.monitoring.overview import OverviewService
from wind_hub_server.application.monitoring.quality import QualityService
from wind_hub_server.application.operation.registry import OperationRecord
from wind_hub_server.application.sink.service import SinkService
from wind_hub_server.application.task.control import TaskControlService
from wind_hub_server.application.task.model import TaskInstanceDetail, TaskSummary
from wind_hub_server.application.worker.registry import WorkerRegistry

T = TypeVar("T")


def devices(ctx: AppContext) -> DeviceQueryService:
    """返回 V1 DeviceQueryService；未装配时按服务不可用处理。"""
    if ctx.devices is None:
        raise APIError("SERVICE_UNAVAILABLE", "device use case is not configured", 503)
    return ctx.devices


def device_data(ctx: AppContext) -> DeviceDataService:
    """返回 Devices Data/Trend 服务。"""
    if ctx.device_data is None:
        raise APIError("SERVICE_UNAVAILABLE", "device data use case is not configured", 503)
    return ctx.device_data


def device_control(ctx: AppContext) -> DeviceCommandService:
    """返回设备控制与回读服务。"""
    if ctx.device_control is None:
        raise APIError("SERVICE_UNAVAILABLE", "device control use case is not configured", 503)
    return ctx.device_control


def admin_state(ctx: AppContext) -> AdminStateService:
    if ctx.admin_state is None:
        raise APIError("SERVICE_UNAVAILABLE", "admin state is not configured", 503)
    return ctx.admin_state


def config_admin(ctx: AppContext) -> ConfigFileService:
    if ctx.config_admin is None:
        raise APIError("SERVICE_UNAVAILABLE", "config admin is not configured", 503)
    return ctx.config_admin


def settings(ctx: AppContext) -> SettingsService:
    if ctx.settings is None:
        raise APIError("SERVICE_UNAVAILABLE", "settings use case is not configured", 503)
    return ctx.settings


def definitions(ctx: AppContext) -> DefinitionQueryService:
    if ctx.definitions is None:
        raise APIError("SERVICE_UNAVAILABLE", "definitions use case is not configured", 503)
    return ctx.definitions


def sinks(ctx: AppContext) -> SinkService:
    if ctx.sinks is None:
        raise APIError("SERVICE_UNAVAILABLE", "sink use case is not configured", 503)
    return ctx.sinks


def diagnostics(ctx: AppContext) -> DiagnosticService:
    if ctx.diagnostics is None:
        raise APIError("SERVICE_UNAVAILABLE", "diagnostics use case is not configured", 503)
    return ctx.diagnostics


def quality(ctx: AppContext) -> QualityService:
    if ctx.quality is None:
        raise APIError("SERVICE_UNAVAILABLE", "quality use case is not configured", 503)
    return ctx.quality


def logs(ctx: AppContext) -> LogQueryService:
    if ctx.logs is None:
        raise APIError("SERVICE_UNAVAILABLE", "logs use case is not configured", 503)
    return ctx.logs


def system_health(ctx: AppContext) -> SystemHealthService:
    if ctx.system_health is None:
        raise APIError("SERVICE_UNAVAILABLE", "system health use case is not configured", 503)
    return ctx.system_health


def tasks(ctx: AppContext) -> TaskControlService:
    """返回 Task 控制服务；未装配时按服务不可用处理。"""
    if ctx.tasks is None:
        raise APIError("SERVICE_UNAVAILABLE", "tasks use case is not configured", 503)
    return ctx.tasks


def workers(ctx: AppContext) -> WorkerRegistry:
    """返回 Worker Registry；未装配时按服务不可用处理。"""
    if ctx.workers is None:
        raise APIError("SERVICE_UNAVAILABLE", "worker registry is not configured", 503)
    return ctx.workers


def overview(ctx: AppContext) -> OverviewService:
    """返回 OverviewService；未装配时按服务不可用处理。"""
    if ctx.overview is None:
        raise APIError("SERVICE_UNAVAILABLE", "overview use case is not configured", 503)
    return ctx.overview


def page(items: list[T], page: int, page_size: int) -> tuple[list[T], PageMeta]:
    """对已完成过滤的内存快照执行稳定分页。"""
    total = len(items)
    start = (page - 1) * page_size
    return items[start : start + page_size], PageMeta(
        page=page, page_size=page_size, total=total
    )


def device_response(row: DeviceSnapshot) -> DeviceResponse:
    """Application DeviceSnapshot 转 API DTO。"""
    return DeviceResponse(**row.model_dump())


def task_response(row: TaskSummary) -> TaskResponse:
    """Application TaskSummary 转 API DTO。"""
    return TaskResponse(**row.model_dump())


def instance_response(row: TaskInstanceDetail) -> TaskInstanceResponse:
    """Application TaskInstanceDetail 转 API DTO。"""
    data = row.model_dump()
    data["state"] = row.state.value
    return TaskInstanceResponse(**data)


def operation_response(row: OperationRecord) -> OperationResponse:
    """Application OperationRecord 转 API DTO。"""
    data = row.model_dump()
    data["state"] = row.state.value
    return OperationResponse(**data)


def trend_response(row: TrendSeries) -> TrendSeriesResponse:
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
