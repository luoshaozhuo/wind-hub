"""Typed gRPC boundary for the 13 published Sink Service RPC contracts."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2, common_pb2_grpc, sink_service_pb2, sink_service_pb2_grpc
from sink_service.application.config_manager import ConfigManager
from sink_service.application.delivery_registry import DeliveryRegistry
from sink_service.infrastructure.worker_registry import WorkerRegistryView, DeliveryView
from sink_service.application.observability import Observability
from sink_service.domain.config import (
    CandidateNotFoundError,
    ConfigurationError,
    InvalidConfigurationError,
    RevisionConflictError,
    ValidatedSnapshot,
    WriterRequiredError,
)
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


def _status_for(exc: ConfigurationError) -> grpc.StatusCode:
    """Map the domain error taxonomy to the gRPC status contract (section 9.3.1)."""
    if isinstance(exc, InvalidConfigurationError):
        return grpc.StatusCode.INVALID_ARGUMENT
    if isinstance(exc, RevisionConflictError):
        return grpc.StatusCode.ABORTED
    if isinstance(exc, WriterRequiredError):
        return grpc.StatusCode.PERMISSION_DENIED
    if isinstance(exc, CandidateNotFoundError):
        return grpc.StatusCode.NOT_FOUND
    return grpc.StatusCode.FAILED_PRECONDITION


def _peer_identity(context: grpc.aio.ServicerContext[Any, Any]) -> str:
    """Use only the mTLS certificate SAN; never a self-declared request field."""
    identities = (context.auth_context() or {}).get(
        'x509_subject_alternative_name', ()
    )
    for raw in identities:
        identity = raw.decode('utf-8', errors='strict')
        if identity.startswith('spiffe://'):
            return identity
    return 'unknown-authenticated-peer'

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
        registry: DeliveryRegistry | WorkerRegistryView,
        *,
        service_id: str,
        role: str,
        observability: Observability,
    ) -> None:
        self._observability = observability
        self._config = config
        self._registry = registry
        self._service_id = service_id
        self._role = role
        self._started_at = datetime.now(UTC)
        self._diagnose_slots = asyncio.Semaphore(4)

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
        degraded = any(
            view.actual_state
            != ("RUNNING" if view.desired_state == "START" else "STOPPED")
            or bool(view.last_error)
            for view in views
        )
        workers = getattr(self._registry, "worker_health", {})
        worker_unavailable = any(value != "RUNNING" for value in workers.values())
        configuration_pending = (
            self._config.disk.revision != self._config.runtime.revision
        )
        health = (
            "DEGRADED"
            if degraded or worker_unavailable or self._config.last_error or configuration_pending
            else "HEALTHY"
        )
        return common_pb2.GetServiceStatusResponse(
            health=health,
            dependencies=[
                common_pb2.DependencyHealth(
                    name="configuration",
                    health="PENDING" if configuration_pending else "RUNNING",
                    detail=self._config.last_error,
                ),
                *[
                    common_pb2.DependencyHealth(
                        name=view.delivery_id,
                        health=view.actual_state,
                        detail=view.last_error,
                    )
                    for view in views
                ],
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
            protocols=["nats_core", "jetstream", "redis", "kafka", "modbus_tcp"],
            features=[
                "full_snapshot_configuration",
                "per_delivery_lifecycle",
                "realtime_latest_value",
                "realtime_batch_output",
                "durable_jetstream_consumer",
                "dlq_puback_before_ack",
                "three_process_isolation",
            ],
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
                await _abort(context, _status_for(exc), str(exc))
                raise AssertionError("gRPC abort returned") from exc
        else:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid snapshot scope")
            raise AssertionError("gRPC abort returned")
        application = self._config.latest_application
        if application is not None and application.file_commit == snapshot.revision:
            apply_results = [
                common_pb2.ApplicationResult(object_id=name, status="APPLIED")
                for name in application.applied
            ] + [
                common_pb2.ApplicationResult(
                    object_id=name,
                    status="FAILED" if application.error else "PENDING",
                    message=application.error,
                )
                for name in application.pending
            ]
            application_id = application.application_id
        else:
            apply_results = []
            application_id = ""
        return common_pb2.GetConfigSnapshotResponse(
            snapshot_id=snapshot.revision,
            revision=snapshot.revision,
            files=_files(snapshot),
            application_id=application_id,
            apply_results=apply_results,
            last_error=self._config.last_error,
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
        except InvalidConfigurationError as exc:
            self._observability.event(
                'ERROR', 'configuration', 'CONFIG_PUSH_REJECTED',
                f'caller={_peer_identity(context)} reason={exc}',
            )
            reason = str(exc)
            path, separator, message = reason.partition(':')
            return common_pb2.PushConfigSnapshotResponse(
                candidate_id='',
                valid=False,
                base_revision=self._config.disk.revision,
                validation=[
                    common_pb2.ValidationIssue(
                        path=path if separator else '',
                        code='INVALID_CONFIGURATION',
                        message=message.strip() if separator else reason,
                    )
                ],
            )
        except ConfigurationError as exc:
            self._observability.event(
                'ERROR', 'configuration', 'CONFIG_PUSH_REJECTED',
                f'caller={_peer_identity(context)} reason={exc}',
            )
            await _abort(context, _status_for(exc), str(exc))
            raise AssertionError('gRPC abort returned') from exc
        self._observability.event(
            'INFO', 'configuration', 'CONFIG_PUSH',
            f'caller={_peer_identity(context)} candidate={candidate_id} '
            f'base_revision={self._config.disk.revision}',
        )
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
            applied = await self._config.apply(request.candidate_id, request.expected_revision)
        except ConfigurationError as exc:
            await _abort(context, _status_for(exc), str(exc))
            raise AssertionError("gRPC abort returned") from exc
        self._observability.event(
            'INFO' if not applied.error else 'ERROR',
            'configuration', 'CONFIG_APPLY',
            f'caller={_peer_identity(context)} application={applied.application_id} '
            f'file_commit={applied.file_commit} restart_required={applied.restart_required} '
            f'error={applied.error}',
        )
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
        settings = self._config.runtime.service["metrics"]
        supported = settings["supported_window_ms"]
        if request.window_ms not in supported:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "unsupported metrics window")
            raise AssertionError("gRPC abort returned")
        if request.scope not in ("", "service", "delivery"):
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "unsupported metrics scope")
            raise AssertionError("gRPC abort returned")
        if request.scope == "delivery":
            delivery_data = self._observability.delivery_snapshot(request.window_ms)
            data = {
                f"delivery.{identifier}.{name}": value
                for identifier, metrics in sorted(delivery_data.items())
                for name, value in metrics.items()
            }
        else:
            data = self._observability.snapshot(request.window_ms)
        stamp = _timestamp(
            self._observability.computed_at or self._observability.started_at
        )
        units = {
            'received_total': 'samples',
            'delivered_total': 'samples',
            'failed_total': 'attempts',
            'dropped_total': 'samples',
            'dead_letter_total': 'samples',
            'delivered_rate_per_second': 'samples/s',
            'write_success_ratio': 'ratio',
            'write_latency_ms_p99': 'ms',
            'backlog_messages': 'messages',
            'io_inflight': 'operations',
            'metrics_snapshot_ready': 'bool',
            'metrics_covered_ms': 'ms',
        }
        return common_pb2.GetMetricsResponse(
            metrics=[
                common_pb2.Metric(
                    name=name,
                    value=value,
                    unit=units.get(name.rsplit(".", 1)[-1], "count"),
                    window_ms=request.window_ms, sampled_at=stamp,
                )
                for name, value in sorted(data.items())
            ]
        )

    async def GetEvents(
        self, request: common_pb2.GetEventsRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.GetEventsResponse:
        limit = request.page_size or 100
        if limit > 500:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "page size exceeds 500")
            raise AssertionError("gRPC abort returned")
        start = request.from_time.ToDatetime(tzinfo=UTC) if request.HasField("from_time") else None
        end = request.to_time.ToDatetime(tzinfo=UTC) if request.HasField("to_time") else None
        if start is not None and end is not None and start > end:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid event time range")
            raise AssertionError("gRPC abort returned")
        try:
            values, token = self._observability.list_events(
                start=start,
                end=end,
                query=request.filter,
                after_id=request.page_token,
                count=limit,
            )
        except ValueError as exc:
            await _abort(context, grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise AssertionError("gRPC abort returned") from exc
        return common_pb2.GetEventsResponse(
            events=[
                common_pb2.Event(
                    event_id=item.event_id, timestamp=_timestamp(item.timestamp),
                    severity=item.severity, source=item.source,
                    error_code=item.error_code, message=item.message,
                )
                for item in values
            ],
            next_page_token=token,
        )

    async def Diagnose(
        self, request: common_pb2.DiagnoseRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common_pb2.DiagnoseResponse:
        allowed = {'configuration', 'source', 'consumer', 'delivery', 'sink', 'target'}
        requested = list(request.stages) or sorted(allowed)
        budget_ms = request.timeout_ms or 5000
        if not 1 <= budget_ms <= 5000 or len(requested) > len(allowed):
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, 'invalid diagnostic bounds')
            raise AssertionError('gRPC abort returned')
        if any(name not in allowed for name in requested) or len(set(requested)) != len(requested):
            await _abort(
                context, grpc.StatusCode.INVALID_ARGUMENT,
                'unknown or duplicate diagnostic stage',
            )
            raise AssertionError('gRPC abort returned')
        views = self._registry.list()
        if request.target_id:
            views = tuple(view for view in views if view.delivery_id == request.target_id)
            if not views:
                await _abort(context, grpc.StatusCode.NOT_FOUND, 'unknown Delivery')
                raise AssertionError('gRPC abort returned')
        try:
            await asyncio.wait_for(self._diagnose_slots.acquire(), timeout=budget_ms / 1000)
        except TimeoutError as exc:
            await _abort(context, grpc.StatusCode.RESOURCE_EXHAUSTED, 'diagnostic capacity')
            raise AssertionError('gRPC abort returned') from exc
        try:
            results: list[common_pb2.DiagnosticStage] = []
            limitations: list[str] = []
            for stage in requested:
                began = time.perf_counter()
                observations: list[tuple[str, str]] = []
                if stage == 'configuration':
                    error = self._config.last_error
                    active = self._config.runtime.revision
                    committed = self._config.disk.revision
                    observations.append(('active_revision', active))
                    observations.append(('committed_revision', committed))
                    if error:
                        status = 'FAILED'
                        message = error
                    elif active != committed:
                        status = 'UNKNOWN'
                        message = 'Committed configuration is not fully active'
                        limitations.append(message)
                    else:
                        status = 'SUCCESS'
                        message = ''
                elif stage == 'target':
                    status = 'NOT_SUPPORTED'
                    message = 'No side-effect-free target protocol probe configured'
                    limitations.append(message)
                else:
                    for view in views:
                        evidence = self._registry.inspect(view.delivery_id)
                        if stage == 'delivery':
                            state = view.actual_state
                            observations.append((view.delivery_id, state))
                        elif stage in evidence:
                            observations.append((view.delivery_id, evidence[stage]))
                    if not observations:
                        status = 'UNKNOWN'
                        message = 'No authoritative process-owned evidence'
                        limitations.append(f'{stage}: no observation')
                    elif any(value in {'FAILED', 'DISCONNECTED'} for _, value in observations):
                        status = 'FAILED'
                        message = 'Observed failure in process-owned state'
                    elif any(
                        value in {'NOT_ATTACHED', 'NOT_BOUND', 'STOPPED'}
                        for _, value in observations
                    ):
                        status = 'UNKNOWN'
                        message = 'Target is not currently bound or active'
                        limitations.append(f'{stage}: incomplete active evidence')
                    else:
                        status = 'SUCCESS'
                        message = 'Confirmed process-local observation only'
                results.append(
                    common_pb2.DiagnosticStage(
                        stage=stage, status=status, message=message,
                        duration_ms=int((time.perf_counter() - began) * 1000),
                        source=self._service_id,
                        evidence=[
                            common_pb2.Evidence(name=key, value=value)
                            for key, value in observations
                        ],
                        observed_at=_timestamp(datetime.now(UTC)),
                    )
                )
            overall = (
                'FAILED' if any(s.status == 'FAILED' for s in results)
                else 'PARTIAL' if any(s.status != 'SUCCESS' for s in results)
                else 'SUCCESS'
            )
            return common_pb2.DiagnoseResponse(
                overall_status=overall, stages=results, limitations=limitations,
            )
        finally:
            self._diagnose_slots.release()

class SinkServiceServer(sink_service_pb2_grpc.SinkServiceServicer):
    """Delivery queries and operator commands through a single Writer."""

    def __init__(
        self,
        registry: DeliveryRegistry | WorkerRegistryView,
        config: ConfigManager,
        observability: Observability,
    ) -> None:
        self._observability = observability
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
        if request.channel not in ("", "realtime", "reliable"):
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "invalid Delivery channel")
            raise AssertionError("gRPC abort returned")
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
            next_page_token=(str(offset + limit) if len(values) > offset + limit else ""),
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
            self._observability.event(
                'ERROR', request.id, 'DELIVERY_INTENT_REJECTED',
                f'caller={_peer_identity(context)} reason={exc}',
            )
            await _abort(context, _status_for(exc), str(exc))
            raise AssertionError("gRPC abort returned") from exc

        desired = "START" if start else "STOP"
        self._observability.event(
            'INFO', request.id, 'DELIVERY_INTENT',
            f'caller={_peer_identity(context)} desired={desired} revision={snapshot.revision}',
        )
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
    registry: DeliveryRegistry | WorkerRegistryView,
    service_id: str,
    role: str,
    credentials: grpc.ServerCredentials,
    address: str,
    max_active_rpcs: int,
    max_request_bytes: int,
    callers: object,
    observability: Observability,
) -> grpc.aio.Server:
    server = grpc.aio.server(
        interceptors=[PeerAuthorization(callers)],
        maximum_concurrent_rpcs=max_active_rpcs,
        options=[("grpc.max_receive_message_length", max_request_bytes)],
    )
    common_pb2_grpc.add_CommonServiceServicer_to_server(
        CommonServiceServer(
            config, registry, service_id=service_id, role=role,
            observability=observability,
        ), server
    )
    sink_service_pb2_grpc.add_SinkServiceServicer_to_server(
        SinkServiceServer(registry, config, observability), server
    )
    if server.add_secure_port(address, credentials) == 0:
        raise ConfigurationError(f"cannot bind secure gRPC endpoint: {address}")
    return server
