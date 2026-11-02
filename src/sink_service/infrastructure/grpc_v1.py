"""Typed gRPC boundary for the 13 published Sink Service RPC contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2, common_pb2_grpc, sink_service_pb2, sink_service_pb2_grpc
from sink_service.application.config_manager import ConfigManager
from sink_service.application.delivery_registry import DeliveryRegistry, DeliveryView
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot
from sink_service.infrastructure.grpc_auth import PeerAuthorization


def _timestamp(now: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(now)
    return result


def _files(snapshot: ValidatedSnapshot) -> list[common_pb2.SnapshotFile]:
    return [
        common_pb2.SnapshotFile(
            relative_path=item.path,
            content=item.content,
            size_bytes=len(item.content),
            sha256=sha256(item.content).hexdigest(),
        )
        for item in snapshot.files
    ]


async def _abort(
    context: grpc.aio.ServicerContext[Any, Any],
    code: grpc.StatusCode,
    message: str,
) -> None:
    await context.abort(code, message)


def _delivery(
    value: DeliveryView, configuration: dict[str, Any] | None = None
) -> sink_service_pb2.Delivery:
    fields = [
        common_pb2.AddressField(
            key=key,
            value=common_pb2.ScalarValue(
                string_value=json.dumps(item, ensure_ascii=False, sort_keys=True)
            ),
        )
        for key, item in sorted((configuration or {}).items())
    ]
    return sink_service_pb2.Delivery(
        delivery_id=value.delivery_id,
        channel=value.channel,
        sink_id=value.sink_id,
        process_role=value.process_role,
        desired_state=value.desired_state,
        actual_state=value.actual_state,
        revision=value.revision,
        last_error=value.last_error,
        config=fields,
    )


class CommonServiceServer(common_pb2_grpc.CommonServiceServicer):
    """CommonService with explicit unsupported-stage behavior."""

    def __init__(
        self,
        config: ConfigManager,
        registry: DeliveryRegistry,
        *,
        service_id: str,
        role: str,
    ) -> None:
        self._config = config
        self._registry = registry
        self._service_id = service_id
        self._role = role
        self._started_at = datetime.now(UTC)

    async def GetServiceInfo(
        self, request: common_pb2.Empty, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.GetServiceInfoResponse:
        return common_pb2.GetServiceInfoResponse(
            service_id=self._service_id,
            process_role=self._role,
            software_version="1.0.0",
            contract_version="v1",
            started_at=_timestamp(self._started_at),
        )

    async def GetServiceStatus(
        self, request: common_pb2.Empty, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.GetServiceStatusResponse:
        views = self._registry.list()
        failed = [view.delivery_id for view in views if view.actual_state == "FAILED"]
        health = "DEGRADED" if failed or self._config.last_error else "HEALTHY"
        return common_pb2.GetServiceStatusResponse(
            health=health,
            dependencies=[
                common_pb2.DependencyHealth(
                    name=view.delivery_id,
                    health="FAILED" if view.delivery_id in failed else view.actual_state,
                    detail=view.last_error,
                )
                for view in views
            ],
            observed_at=_timestamp(datetime.now(UTC)),
        )

    async def GetCapabilities(
        self, request: common_pb2.Empty, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.GetCapabilitiesResponse:
        return common_pb2.GetCapabilitiesResponse(
            rpc_names=[
                "GetServiceInfo",
                "GetServiceStatus",
                "GetCapabilities",
                "GetConfigSnapshot",
                "PushConfigSnapshot",
                "ApplyConfigSnapshot",
                "GetMetrics",
                "GetEvents",
                "Diagnose",
                "ListDeliveries",
                "GetDelivery",
                "StartDelivery",
                "StopDelivery",
            ],
            protocols=["nats", "redis", "kafka", "modbus_tcp"],
            features=["full_snapshot_configuration", "per_delivery_lifecycle"],
        )

    async def GetConfigSnapshot(
        self,
        request: common_pb2.GetConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common_pb2.GetConfigSnapshotResponse:
        if request.scope == common_pb2.SNAPSHOT_SCOPE_DISK:
            snapshot = self._config.disk
        elif request.scope == common_pb2.SNAPSHOT_SCOPE_RUNTIME:
            snapshot = self._config.runtime
        elif request.scope == common_pb2.SNAPSHOT_SCOPE_CANDIDATE:
            try:
                snapshot = self._config.candidate(request.candidate_id)
            except ConfigurationError as exc:
                await _abort(context, grpc.StatusCode.NOT_FOUND, str(exc))
                raise AssertionError("gRPC abort returned") from exc
        else:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid snapshot scope")
            raise AssertionError("gRPC abort returned")
        return common_pb2.GetConfigSnapshotResponse(
            snapshot_id=snapshot.revision,
            revision=snapshot.revision,
            files=_files(snapshot),
        )

    async def PushConfigSnapshot(
        self,
        request: common_pb2.PushConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common_pb2.PushConfigSnapshotResponse:
        try:
            candidate_id, _snapshot = await self._config.push(
                [
                    (item.relative_path, bytes(item.content), item.size_bytes, item.sha256)
                    for item in request.files
                ],
                request.expected_revision,
            )
        except ConfigurationError as exc:
            await _abort(context, grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise AssertionError("gRPC abort returned") from exc
        return common_pb2.PushConfigSnapshotResponse(
            candidate_id=candidate_id,
            valid=True,
            base_revision=self._config.disk.revision,
        )

    async def ApplyConfigSnapshot(
        self,
        request: common_pb2.ApplyConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common_pb2.ApplyConfigSnapshotResponse:
        try:
            applied = await self._config.apply(
                request.candidate_id, request.expected_revision
            )
        except ConfigurationError as exc:
            await _abort(context, grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise AssertionError("gRPC abort returned") from exc
        return common_pb2.ApplyConfigSnapshotResponse(
            file_commit=applied.file_commit,
            effective_revision=applied.effective_revision,
            restart_required=applied.restart_required,
            application_id=applied.application_id,
            apply_results=(
                [
                    common_pb2.ApplicationResult(object_id=name, status="APPLIED")
                    for name in applied.applied
                ]
                + [
                    common_pb2.ApplicationResult(
                        object_id=name,
                        status="FAILED" if applied.error else "PENDING",
                        message=applied.error,
                    )
                    for name in applied.pending
                ]
            ),
        )

    async def GetMetrics(
        self, request: common_pb2.GetMetricsRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.GetMetricsResponse:
        await _abort(context, grpc.StatusCode.UNIMPLEMENTED, "metrics belong to stage four")
        raise AssertionError("gRPC abort returned")

    async def GetEvents(
        self, request: common_pb2.GetEventsRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.GetEventsResponse:
        await _abort(context, grpc.StatusCode.UNIMPLEMENTED, "events belong to stage four")
        raise AssertionError("gRPC abort returned")

    async def Diagnose(
        self, request: common_pb2.DiagnoseRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.DiagnoseResponse:
        await _abort(context, grpc.StatusCode.UNIMPLEMENTED, "diagnostics belong to stage four")
        raise AssertionError("gRPC abort returned")


class SinkServiceServer(sink_service_pb2_grpc.SinkServiceServicer):
    """Delivery queries and operator commands through a single Writer."""

    def __init__(self, registry: DeliveryRegistry, config: ConfigManager) -> None:
        self._registry = registry
        self._config = config

    async def ListDeliveries(
        self,
        request: sink_service_pb2.ListDeliveriesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> sink_service_pb2.ListDeliveriesResponse:
        try:
            offset = int(request.page_token) if request.page_token else 0
            if offset < 0 or str(offset) != (request.page_token or "0"):
                raise ValueError("invalid page token")
        except ValueError as exc:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise AssertionError("gRPC abort returned") from exc
        values = self._registry.list(request.channel)
        limit = request.page_size or 100
        if limit > 500:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "page size exceeds 500")
            raise AssertionError("gRPC abort returned")
        return sink_service_pb2.ListDeliveriesResponse(
            deliveries=[
                _delivery(view, self._config.disk.deliveries.get(view.delivery_id))
                for view in values[offset : offset + limit]
            ],
            next_page_token=(
                str(offset + limit) if len(values) > offset + limit else ""
            ),
        )

    async def GetDelivery(
        self, request: common_pb2.ObjectId, context: grpc.aio.ServicerContext[Any, Any]
    ) -> sink_service_pb2.GetDeliveryResponse:
        try:
            return sink_service_pb2.GetDeliveryResponse(
                delivery=_delivery(
                    self._registry.get(request.id),
                    self._config.disk.deliveries.get(request.id),
                )
            )
        except KeyError as exc:
            await _abort(context, grpc.StatusCode.NOT_FOUND, str(exc))
            raise AssertionError("gRPC abort returned") from exc

    async def _operate(
        self,
        request: common_pb2.OperationRequest,
        context: grpc.aio.ServicerContext[Any, Any],
        *,
        start: bool,
    ) -> common_pb2.OperationResult:
        try:
            # The Writer owns intent for both roles, even when the Reader runs
            # the actual reliable Delivery.
            snapshot, _commit = await self._config.set_delivery_desired(
                request.id,
                running=start,
                expected_revision=request.expected_revision,
            )
        except KeyError as exc:
            await _abort(context, grpc.StatusCode.NOT_FOUND, str(exc))
            raise AssertionError("gRPC abort returned") from exc
        except ConfigurationError as exc:
            await _abort(context, grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise AssertionError("gRPC abort returned") from exc

        desired = "START" if start else "STOP"
        try:
            view = self._registry.get(request.id)
        except KeyError:
            # The other process will reconcile the newly committed revision.
            return common_pb2.OperationResult(
                operation_id=snapshot.revision,
                accepted=True,
                desired_state=desired,
                actual_state="PENDING",
                revision=snapshot.revision,
            )
        return common_pb2.OperationResult(
            operation_id=snapshot.revision,
            accepted=True,
            desired_state=view.desired_state,
            actual_state=view.actual_state,
            revision=snapshot.revision,
            error_code="RUNTIME_FAILED" if view.last_error else "",
            message=view.last_error,
        )

    async def StartDelivery(
        self, request: common_pb2.OperationRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.OperationResult:
        return await self._operate(request, context, start=True)

    async def StopDelivery(
        self, request: common_pb2.OperationRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.OperationResult:
        return await self._operate(request, context, start=False)


def build_v1_server(
    *,
    config: ConfigManager,
    registry: DeliveryRegistry,
    service_id: str,
    role: str,
    credentials: grpc.ServerCredentials,
    address: str,
    max_active_rpcs: int,
    max_request_bytes: int,
    callers: object,
) -> grpc.aio.Server:
    server = grpc.aio.server(
        interceptors=[PeerAuthorization(callers)],
        maximum_concurrent_rpcs=max_active_rpcs,
        options=[("grpc.max_receive_message_length", max_request_bytes)],
    )
    common_pb2_grpc.add_CommonServiceServicer_to_server(
        CommonServiceServer(config, registry, service_id=service_id, role=role), server
    )
    sink_service_pb2_grpc.add_SinkServiceServicer_to_server(
        SinkServiceServer(registry, config), server
    )
    if server.add_secure_port(address, credentials) == 0:
        raise ConfigurationError(f"cannot bind secure gRPC endpoint: {address}")
    return server
