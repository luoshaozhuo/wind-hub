"""IndustrialGatewayService v1: device management and independent variable updates."""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import industrial_gateway_pb2 as pb
from contracts import industrial_gateway_pb2_grpc as rpc
from core.application.errors import ConfigError
from contracts.scalar_codec import decode_scalar, encode_scalar
from industrial_gateway.infrastructure.config.snapshots import ConfigSnapshots
from industrial_gateway.infrastructure.config.v1_schema import service_path
from industrial_gateway.infrastructure.diagnostics import GatewayDiagnostics
from industrial_gateway.infrastructure.runtime import GatewayInstance, GatewayRuntime
from industrial_gateway.infrastructure.variable_leases import LeaseError

_PAGE_LIMIT = 500


async def _authorize(
    context: grpc.aio.ServicerContext[Any, Any],
    runtime: GatewayRuntime,
    permission: str,
) -> None:
    """Enforce mTLS identity permissions or explicitly local-only development."""
    policy = runtime.config.service.grpc
    if not policy.tls.enabled:
        if context.peer().startswith(("ipv4:127.0.0.1:", "ipv6:[::1]:")):
            return
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "loopback-only deployment")
        return
    common_names = context.auth_context().get("x509_common_name", ())
    identity = common_names[0].decode("utf-8") if common_names else ""
    rights = policy.permissions.get(identity, ())
    if permission not in rights:
        await context.abort(
            grpc.StatusCode.PERMISSION_DENIED,
            "caller identity has insufficient permissions",
        )


def _trusted_identity(
    context: grpc.aio.ServicerContext[Any, Any],
    runtime: GatewayRuntime,
) -> str:
    """Use authenticated client certificate CN, never client-supplied metadata."""
    if not runtime.config.service.grpc.tls.enabled:
        return "local-loopback"
    identities = context.auth_context().get("x509_common_name", ())
    return identities[0].decode("utf-8") if identities else "unidentified"


def _timestamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value)
    return result


def _page(keys: list[str], page_size: int, page_token: str) -> tuple[list[str], str]:
    if page_token:
        try:
            start = int(page_token)
        except ValueError as exc:
            raise ValueError("page_token must be an integer offset") from exc
    else:
        start = 0
    if start < 0 or start > len(keys):
        raise ValueError("page_token out of range")
    limit = min(page_size or 100, _PAGE_LIMIT)
    end = min(start + limit, len(keys))
    return keys[start:end], str(end) if end < len(keys) else ""


class GatewayGrpcService(rpc.IndustrialGatewayServiceServicer):
    """The eight canonical RPCs defined in industrial_gateway.proto."""

    def __init__(self, runtime: GatewayRuntime) -> None:
        self._runtime = runtime

    async def _instance(
        self, gateway_id: str, context: grpc.aio.ServicerContext[Any, Any]
    ) -> GatewayInstance:
        instance = self._runtime.instances.get(gateway_id)
        if instance is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown gateway_id")
            raise RuntimeError("abort unexpectedly returned") from None
        return instance

    def _gateway(self, gateway_id: str, instance: GatewayInstance) -> pb.Gateway:
        config = instance.config
        status = instance.lifecycle.status
        return pb.Gateway(
            gateway_id=gateway_id,
            endpoint=(f"{config.settings.listen_host}:{config.settings.listen_port}"),
            mapping_summary=f"{len(config.variables)} variables",
            desired_state=status.desired.value,
            actual_state=status.actual.value,
            revision=self._runtime.config.revision,
            last_error=status.last_error,
        )

    async def ListGateways(
        self,
        request: pb.ListGatewaysRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.ListGatewaysResponse:
        await _authorize(context, self._runtime, "read")
        ids = sorted(
            gateway_id
            for gateway_id in self._runtime.instances
            if not request.filter or request.filter in gateway_id
        )
        try:
            selected, next_token = _page(ids, request.page_size, request.page_token)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc
        return pb.ListGatewaysResponse(
            gateways=[self._gateway(key, self._runtime.instances[key]) for key in selected],
            next_page_token=next_token,
        )

    async def GetGateway(
        self,
        request: common.ObjectId,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.GetGatewayResponse:
        await _authorize(context, self._runtime, "read")
        instance = await self._instance(request.id, context)
        return pb.GetGatewayResponse(gateway=self._gateway(request.id, instance))

    async def StartGateway(
        self,
        request: common.OperationRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.OperationResult:
        await _authorize(context, self._runtime, "lifecycle")
        instance = await self._instance(request.id, context)
        if not request.expected_revision:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "expected_revision is required",
            )
        if request.expected_revision != self._runtime.config.revision:
            await context.abort(grpc.StatusCode.ABORTED, "revision mismatch")
        try:
            await self._runtime.start_requested(request.id, request.expected_revision)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.ABORTED, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc
        except (RuntimeError, OSError) as exc:
            return common.OperationResult(
                operation_id=uuid4().hex,
                accepted=False,
                desired_state=instance.lifecycle.status.desired.value,
                actual_state=instance.lifecycle.status.actual.value,
                revision=self._runtime.config.revision,
                error_code="START_FAILED",
                message=str(exc),
            )
        return common.OperationResult(
            operation_id=uuid4().hex,
            accepted=True,
            desired_state=instance.lifecycle.status.desired.value,
            actual_state=instance.lifecycle.status.actual.value,
            revision=self._runtime.config.revision,
        )

    async def StopGateway(
        self,
        request: common.OperationRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.OperationResult:
        await _authorize(context, self._runtime, "lifecycle")
        instance = await self._instance(request.id, context)
        if not request.expected_revision:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "expected_revision is required",
            )
        if request.expected_revision != self._runtime.config.revision:
            await context.abort(grpc.StatusCode.ABORTED, "revision mismatch")
        try:
            await self._runtime.stop_requested(request.id, request.expected_revision)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.ABORTED, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc
        except (RuntimeError, OSError) as exc:
            return common.OperationResult(
                operation_id=uuid4().hex,
                accepted=False,
                desired_state=instance.lifecycle.status.desired.value,
                actual_state=instance.lifecycle.status.actual.value,
                revision=self._runtime.config.revision,
                error_code="STOP_FAILED",
                message=str(exc),
            )
        return common.OperationResult(
            operation_id=uuid4().hex,
            accepted=True,
            desired_state=instance.lifecycle.status.desired.value,
            actual_state=instance.lifecycle.status.actual.value,
            revision=self._runtime.config.revision,
        )

    async def ListVariables(
        self,
        request: pb.ListVariablesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.ListVariablesResponse:
        await _authorize(context, self._runtime, "read")
        instance = await self._instance(request.gateway_id, context)
        ids = sorted(
            variable_id
            for variable_id in instance.config.variables
            if not request.filter or request.filter in variable_id
        )
        try:
            selected, next_token = _page(ids, request.page_size, request.page_token)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc
        variables: list[pb.Variable] = []
        for variable_id in selected:
            variable = instance.config.variables[variable_id]
            # writable describes gRPC injection, not industrial protocol FC writes.
            writable = True
            variables.append(
                pb.Variable(
                    variable_id=variable_id,
                    name=variable_id,
                    data_type=variable.data_type,
                    readable=True,
                    writable=writable,
                    address=[
                        common.AddressField(key=key, value=encode_scalar(value))
                        for key, value in variable.address.items()
                        if value is not None
                    ],
                )
            )
        return pb.ListVariablesResponse(
            variables=variables,
            next_page_token=next_token,
        )

    async def ReadVariables(
        self,
        request: pb.ReadVariablesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.ReadVariablesResponse:
        await _authorize(context, self._runtime, "read")
        instance = await self._instance(request.gateway_id, context)
        ids = list(request.variable_ids) or list(instance.config.variables)
        queried_at = _timestamp(datetime.now(UTC))
        samples: list[common.Sample] = []
        for variable_id in ids:
            try:
                snapshot = instance.store.read(variable_id)
            except KeyError:
                samples.append(
                    common.Sample(
                        point_id=variable_id,
                        quality="BAD",
                        freshness="UNKNOWN",
                        error_code="NOT_FOUND",
                        error_message="unknown variable",
                        queried_at=queried_at,
                    )
                )
                continue
            sample = common.Sample(
                point_id=variable_id,
                quality="GOOD" if snapshot.value is not None else "UNKNOWN",
                freshness="CURRENT" if snapshot.value is not None else "UNINITIALIZED",
                queried_at=queried_at,
            )
            if snapshot.value is not None:
                sample.value.CopyFrom(encode_scalar(snapshot.value))
            if snapshot.updated_at is not None:
                sample.sample_timestamp.CopyFrom(_timestamp(snapshot.updated_at))
            samples.append(sample)
        return pb.ReadVariablesResponse(
            gateway_id=request.gateway_id,
            values=samples,
        )

    async def ClaimTargets(
        self,
        request: pb.ClaimTargetsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.ClaimTargetsResponse:
        """Acquire, renew, or release source target ownership without PLC I/O."""
        await _authorize(context, self._runtime, "inject")
        await self._instance(request.gateway_id, context)
        try:
            return await self._runtime.claim_targets(request)
        except KeyError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "Gateway no longer exists")
            raise RuntimeError("abort unexpectedly returned") from None

    async def UpdateVariables(
        self,
        request: pb.UpdateVariablesRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> pb.UpdateVariablesResponse:
        await _authorize(context, self._runtime, "inject")
        await self._instance(request.gateway_id, context)
        start = perf_counter()
        values = [
            (
                item.variable_id,
                decode_scalar(item.value)
                if item.HasField("value") and item.value.WhichOneof("kind") is not None
                else None,
            )
            for item in request.values
        ]
        try:
            outcomes = await self._runtime.update_variables(
                request.gateway_id, values, request.lease_token
            )
        except LeaseError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc
        except KeyError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "Gateway no longer exists")
            raise RuntimeError("abort unexpectedly returned") from None
        results = [
            pb.VariableUpdateResult(
                variable_id=item.variable_id,
                success=item.success,
                error_code=item.error_code,
                message=item.message,
            )
            for item in outcomes
        ]
        observations = self._runtime.observability
        observations.record(request.gateway_id, "update_batches_total")
        failed = sum(not item.success for item in results)
        if failed:
            observations.event(
                request.gateway_id,
                f"UpdateVariables completed with {failed} failed items",
                severity="WARNING",
                error_code="PARTIAL_UPDATE_FAILED",
            )
        successes = sum(item.success for item in results)
        if successes:
            observations.record(request.gateway_id, "updated_variables_total", successes)
        observations.latency(request.gateway_id, (perf_counter() - start) * 1000)
        return pb.UpdateVariablesResponse(results=results)


class GatewayCommonService(common_rpc.CommonServiceServicer):
    """Expose the canonical CommonService surface without invented RPC fields."""

    def __init__(self, snapshots: ConfigSnapshots) -> None:
        self._snapshots = snapshots
        self._runtime = snapshots.runtime
        self._diagnostics = GatewayDiagnostics(self._runtime)
        self._started_at = datetime.now(UTC)

    async def GetConfigSnapshot(
        self,
        request: common.GetConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetConfigSnapshotResponse:
        await _authorize(context, self._runtime, "config_read")
        try:
            return await self._snapshots.get(request.scope, request.candidate_id)
        except KeyError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "candidate not found")
            raise RuntimeError("abort unexpectedly returned") from None
        except (ConfigError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc

    async def PushConfigSnapshot(
        self,
        request: common.PushConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.PushConfigSnapshotResponse:
        await _authorize(context, self._runtime, "config_write")
        return await self._snapshots.push(
            list(request.files),
            request.expected_revision,
        )

    async def ApplyConfigSnapshot(
        self,
        request: common.ApplyConfigSnapshotRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.ApplyConfigSnapshotResponse:
        await _authorize(context, self._runtime, "config_write")
        try:
            return await self._snapshots.apply(
                request.candidate_id,
                request.expected_revision,
                operator=_trusted_identity(context, self._runtime),
            )
        except KeyError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "candidate not found")
            raise RuntimeError("abort unexpectedly returned") from None
        except (ConfigError, ValueError) as exc:
            await context.abort(grpc.StatusCode.ABORTED, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc

    async def GetServiceInfo(
        self,
        request: common.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetServiceInfoResponse:
        await _authorize(context, self._runtime, "read")
        response = common.GetServiceInfoResponse(
            service_id="industrial_gateway",
            process_role="gateway",
            software_version="0.1.0",
            contract_version="v1",
        )
        response.started_at.FromDatetime(self._started_at)
        return response

    async def GetServiceStatus(
        self,
        request: common.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetServiceStatusResponse:
        await _authorize(context, self._runtime, "read")
        from industrial_gateway.application.server_lifecycle import ActualState

        dependencies: list[common.DependencyHealth] = []
        unhealthy = False
        for gateway_id, instance in self._runtime.instances.items():
            state = instance.lifecycle.status
            ok = (
                state.actual is ActualState.RUNNING
                if state.desired.value == "START"
                else state.actual is ActualState.STOPPED
            )
            unhealthy |= not ok
            dependencies.append(
                common.DependencyHealth(
                    name=gateway_id,
                    health="HEALTHY" if ok else "UNHEALTHY",
                    detail=(
                        f"{state.actual.value}; "
                        f"restart_count={state.restart_count}; "
                        f"last_failure_at={state.last_failure_at}"
                    ),
                )
            )
        ready = self._runtime.observability.ready
        dependencies.append(
            common.DependencyHealth(
                name="metrics",
                health="HEALTHY" if ready else "NOT_READY",
                detail="periodic snapshot ready" if ready else "first sample pending",
            )
        )
        status = common.GetServiceStatusResponse(
            health="DEGRADED" if unhealthy else "HEALTHY",
            dependencies=dependencies,
        )
        status.observed_at.FromDatetime(datetime.now(UTC))
        return status

    async def GetCapabilities(
        self,
        request: common.Empty,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetCapabilitiesResponse:
        await _authorize(context, self._runtime, "read")
        return common.GetCapabilitiesResponse(
            rpc_names=[
                "CommonService.GetServiceInfo",
                "CommonService.GetServiceStatus",
                "CommonService.GetCapabilities",
                "CommonService.GetConfigSnapshot",
                "CommonService.PushConfigSnapshot",
                "CommonService.ApplyConfigSnapshot",
                "CommonService.GetMetrics",
                "CommonService.GetEvents",
                "CommonService.Diagnose",
                "IndustrialGatewayService.ListGateways",
                "IndustrialGatewayService.GetGateway",
                "IndustrialGatewayService.StartGateway",
                "IndustrialGatewayService.StopGateway",
                "IndustrialGatewayService.ListVariables",
                "IndustrialGatewayService.ReadVariables",
                "IndustrialGatewayService.UpdateVariables",
                "IndustrialGatewayService.ClaimTargets",
            ],
            protocols=["modbus_tcp"],
            features=[
                "single_device_server",
                "grpc_independent_variable_updates",
                "request_level_modbus_write_capture",
                "best_effort_commander_forwarding",
                "service_only_configuration_snapshots",
                "passive_diagnostics",
                "periodic_metrics",
            ],
        )

    async def GetMetrics(
        self,
        request: common.GetMetricsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetMetricsResponse:
        await _authorize(context, self._runtime, "read")
        if request.scope and request.scope not in self._runtime.instances:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown metric scope")
        try:
            return self._runtime.observability.metrics(request.scope, request.window_ms)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc

    async def GetEvents(
        self,
        request: common.GetEventsRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.GetEventsResponse:
        await _authorize(context, self._runtime, "read")
        try:
            return self._runtime.observability.query_events(request)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc

    async def Diagnose(
        self,
        request: common.DiagnoseRequest,
        context: grpc.aio.ServicerContext[Any, Any],
    ) -> common.DiagnoseResponse:
        await _authorize(context, self._runtime, "diagnose")
        try:
            return await self._diagnostics.diagnose(request)
        except KeyError:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown target")
            raise RuntimeError("abort unexpectedly returned") from None
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("abort unexpectedly returned") from exc


def create_gateway_grpc_server(
    runtime: GatewayRuntime, snapshots: ConfigSnapshots
) -> grpc.aio.Server:
    """Bind the v1 gRPC service with an explicit secure listener policy."""
    config = runtime.config.service.grpc
    server = grpc.aio.server(
        maximum_concurrent_rpcs=config.max_active_rpcs,
        options=[
            ("grpc.max_receive_message_length", config.request_max_bytes),
        ],
    )
    rpc.add_IndustrialGatewayServiceServicer_to_server(GatewayGrpcService(runtime), server)
    common_rpc.add_CommonServiceServicer_to_server(GatewayCommonService(snapshots), server)
    host = config.listen_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    bind = f"{host}:{config.listen_port}"
    if config.tls.enabled:
        if not all((config.tls.cert_file, config.tls.key_file, config.tls.ca_file)):
            raise ValueError("mTLS certificate files are required")
        assert config.tls.cert_file is not None
        assert config.tls.key_file is not None
        assert config.tls.ca_file is not None
        root = runtime.config.project_root
        key = service_path(root, config.tls.key_file).read_bytes()
        cert = service_path(root, config.tls.cert_file).read_bytes()
        ca = service_path(root, config.tls.ca_file).read_bytes()
        credentials = grpc.ssl_server_credentials(
            [(key, cert)],
            root_certificates=ca,
            require_client_auth=True,
        )
        bound = server.add_secure_port(bind, credentials)
    else:
        bound = server.add_insecure_port(bind)
    if bound == 0:
        raise RuntimeError(f"unable to bind Gateway gRPC: {bind}")
    return server
