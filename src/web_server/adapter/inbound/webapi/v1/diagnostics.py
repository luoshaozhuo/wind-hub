"""Admin API v1 Diagnostics 路由：网络探针、协议诊断与点表测试。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from web_server.adapter.inbound.webapi.context import get_ctx
from web_server.adapter.inbound.webapi.errors import APIError
from web_server.adapter.inbound.webapi.v1 import common
from web_server.adapter.inbound.webapi.v1.models import (
    DeviceCommandResponse,
    OperationResponse,
    PingRequest,
    PingResponse,
    PointTableTestRequest,
    PortProbeResponse,
    PortsRequest,
    ProtocolCheckRequest,
    ProtocolCheckResponse,
    ProtocolReadRequest,
    ProtocolReadResponse,
    ProtocolWriteRequest,
    SubnetScanRequest,
)
from web_server.application.app_context import AppContext
from web_server.infra.audit import audit

router = APIRouter()


@router.post("/diagnostics/ping", response_model=PingResponse, tags=["v1-diagnostics"])
async def diagnostic_ping(
    request: PingRequest,
    ctx: AppContext = Depends(get_ctx),
) -> PingResponse:
    result = await common.diagnostics(ctx).ping(request.host, request.timeout)
    return PingResponse(**result.model_dump())


@router.post(
    "/diagnostics/ports",
    response_model=list[PortProbeResponse],
    tags=["v1-diagnostics"],
)
async def diagnostic_ports(
    request: PortsRequest,
    ctx: AppContext = Depends(get_ctx),
) -> list[PortProbeResponse]:
    rows = await common.diagnostics(ctx).ports(request.host, request.ports, request.timeout)
    return [PortProbeResponse(**row.model_dump()) for row in rows]


@router.post("/diagnostics/subnet-scan", response_model=OperationResponse, tags=["v1-diagnostics"])
async def diagnostic_subnet_scan(
    request: SubnetScanRequest,
    ctx: AppContext = Depends(get_ctx),
) -> OperationResponse:
    try:
        operation = common.diagnostics(ctx).start_subnet_scan(
            request.network, timeout=request.timeout, ports=request.ports
        )
    except ValueError as exc:
        raise APIError("VALIDATION_ERROR", str(exc), 422) from exc
    return common.operation_response(operation)


@router.post(
    "/diagnostics/protocol/check",
    response_model=ProtocolCheckResponse,
    tags=["v1-diagnostics"],
)
async def diagnostic_protocol_check(
    request: ProtocolCheckRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ProtocolCheckResponse:
    try:
        connected = await common.diagnostics(ctx).protocol_check(request.device_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown device '{request.device_id}'", 404) from None
    return ProtocolCheckResponse(device_id=request.device_id, connected=connected)


@router.post(
    "/diagnostics/protocol/read",
    response_model=ProtocolReadResponse,
    tags=["v1-diagnostics"],
)
async def diagnostic_protocol_read(
    request: ProtocolReadRequest,
    ctx: AppContext = Depends(get_ctx),
) -> ProtocolReadResponse:
    # 读取失败按来源映射：未知设备/点由 Commander 返回 NOT_FOUND，
    # 经全局 gRPC 错误处理器映射为 404；链路故障映射为 503/504。
    value = await common.diagnostics(ctx).read(request.device_id, request.point_id)
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
async def diagnostic_point_table(
    request: PointTableTestRequest,
    ctx: AppContext = Depends(get_ctx),
) -> OperationResponse:
    try:
        operation = common.diagnostics(ctx).start_point_table_test(request.device_id)
    except KeyError:
        raise APIError("NOT_FOUND", f"unknown device '{request.device_id}'", 404) from None
    return common.operation_response(operation)


@router.post(
    "/diagnostics/protocol/write",
    response_model=DeviceCommandResponse,
    tags=["v1-diagnostics"],
)
async def diagnostic_protocol_write(
    request: ProtocolWriteRequest,
    ctx: AppContext = Depends(get_ctx),
) -> DeviceCommandResponse:
    result = await common.diagnostics(ctx).write(request.device_id, request.point_id, request.value)
    audit(
        "diagnostics.write",
        device_id=request.device_id,
        point_id=request.point_id,
        outcome=str(result.outcome),
        success=result.success,
    )
    return DeviceCommandResponse(**result.model_dump())
