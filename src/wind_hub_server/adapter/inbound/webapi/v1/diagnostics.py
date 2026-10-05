"""Admin API v1 Diagnostics 路由：网络探针、协议诊断与点表测试。"""

from __future__ import annotations

from fastapi import APIRouter

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

router = APIRouter()


@router.post("/diagnostics/ping", response_model=PingResponse, tags=["v1-diagnostics"])
async def diagnostic_ping(request: PingRequest) -> PingResponse:
    result = await common.diagnostics().ping(request.host, request.timeout)
    return PingResponse(**result.model_dump())


@router.post(
    "/diagnostics/ports",
    response_model=list[PortProbeResponse],
    tags=["v1-diagnostics"],
)
async def diagnostic_ports(request: PortsRequest) -> list[PortProbeResponse]:
    rows = await common.diagnostics().ports(request.host, request.ports, request.timeout)
    return [PortProbeResponse(**row.model_dump()) for row in rows]


@router.post("/diagnostics/subnet-scan", response_model=OperationResponse, tags=["v1-diagnostics"])
async def diagnostic_subnet_scan(request: SubnetScanRequest) -> OperationResponse:
    try:
        operation = common.diagnostics().start_subnet_scan(
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
) -> ProtocolCheckResponse:
    try:
        connected = await common.diagnostics().protocol_check(request.device_id)
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
        value = await common.diagnostics().read(request.device_id, request.point_id)
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
        operation = common.diagnostics().start_point_table_test(request.device_id)
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
) -> DeviceCommandResponse:
    result = await common.diagnostics().write(
        request.device_id, request.point_id, request.value
    )
    return DeviceCommandResponse(**result.model_dump())
