"""Admin API v1 Diagnostics 路由：网络探针、协议诊断与点表测试。"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from wind_hub_server.adapter.inbound.webapi.context import get_ctx
from wind_hub_server.adapter.inbound.webapi.errors import APIError
from wind_hub_server.adapter.inbound.webapi.v1 import common
from wind_hub_server.adapter.inbound.webapi.v1.models import (
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
from wind_hub_server.application.app_context import AppContext

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
    try:
        value = await common.diagnostics(ctx).read(request.device_id, request.point_id)
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
async def diagnostic_point_table(
    request: PointTableTestRequest,
    ctx: AppContext = Depends(get_ctx),
) -> OperationResponse:
    try:
        operation = common.diagnostics(ctx).start_point_table_test(request.device_id)
    except KeyError:
        raise APIError(
            "NOT_FOUND", f"unknown device '{request.device_id}'", 404
        ) from None
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
    result = await common.diagnostics(ctx).write(
        request.device_id, request.point_id, request.value
    )
    return DeviceCommandResponse(**result.model_dump())
