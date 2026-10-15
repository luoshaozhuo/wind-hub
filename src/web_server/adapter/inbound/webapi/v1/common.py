"""Admin API v1 共享装配与 Application → API DTO 转换辅助。"""

from __future__ import annotations

from typing import TypeVar

from web_server.adapter.inbound.webapi.errors import APIError
from web_server.adapter.inbound.webapi.v1.models import (
    DeviceResponse,
    OperationResponse,
    PageMeta,
    TaskInstanceResponse,
    TaskResponse,
    TrendSampleResponse,
    TrendSeriesResponse,
)
from web_server.application.app_context import AppContext
from web_server.application.config.admin_state import AdminStateService
from web_server.application.config.definitions import DefinitionQueryService
from web_server.application.config.files import ConfigFileService
from web_server.application.config.settings import SettingsService
from web_server.application.device.command import DeviceCommandService
from web_server.application.device.data import DeviceDataService, TrendSeries
from web_server.application.device.diagnostic import DiagnosticService
from web_server.application.device.query import DeviceQueryService, DeviceSnapshot
from web_server.application.monitoring.health import SystemHealthService
from web_server.application.monitoring.logs import LogQueryService
from web_server.application.monitoring.overview import OverviewService
from web_server.application.monitoring.quality import QualityService
from web_server.application.operation.registry import OperationRecord
from web_server.application.sink.service import SinkService
from web_server.application.task.control import TaskControlService
from web_server.application.task.model import TaskInstanceDetail, TaskSummary
from web_server.application.worker.registry import WorkerRegistry

T = TypeVar("T")


def _require(service: T | None, name: str) -> T:
    """返回已装配的应用服务；缺失时按服务不可用处理。"""
    if service is None:
        raise APIError("SERVICE_UNAVAILABLE", f"{name} is not configured", 503)
    return service


def devices(ctx: AppContext) -> DeviceQueryService:
    return _require(ctx.devices, "devices use case")


def device_data(ctx: AppContext) -> DeviceDataService:
    return _require(ctx.device_data, "device_data use case")


def device_control(ctx: AppContext) -> DeviceCommandService:
    return _require(ctx.device_control, "device_control use case")


def admin_state(ctx: AppContext) -> AdminStateService:
    return _require(ctx.admin_state, "admin_state use case")


def config_admin(ctx: AppContext) -> ConfigFileService:
    return _require(ctx.config_admin, "config_admin use case")


def settings(ctx: AppContext) -> SettingsService:
    return _require(ctx.settings, "settings use case")


def definitions(ctx: AppContext) -> DefinitionQueryService:
    return _require(ctx.definitions, "definitions use case")


def sinks(ctx: AppContext) -> SinkService:
    return _require(ctx.sinks, "sinks use case")


def diagnostics(ctx: AppContext) -> DiagnosticService:
    return _require(ctx.diagnostics, "diagnostics use case")


def quality(ctx: AppContext) -> QualityService:
    return _require(ctx.quality, "quality use case")


def logs(ctx: AppContext) -> LogQueryService:
    return _require(ctx.logs, "logs use case")


def system_health(ctx: AppContext) -> SystemHealthService:
    return _require(ctx.system_health, "system_health use case")


def tasks(ctx: AppContext) -> TaskControlService:
    return _require(ctx.tasks, "tasks use case")


def workers(ctx: AppContext) -> WorkerRegistry:
    return _require(ctx.workers, "workers use case")


def overview(ctx: AppContext) -> OverviewService:
    return _require(ctx.overview, "overview use case")


def page(items: list[T], page: int, page_size: int) -> tuple[list[T], PageMeta]:
    """对已完成过滤的内存快照执行稳定分页。"""
    total = len(items)
    start = (page - 1) * page_size
    return items[start : start + page_size], PageMeta(page=page, page_size=page_size, total=total)


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
