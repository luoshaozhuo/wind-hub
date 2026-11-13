"""Public Data Generator and Common unary gRPC services."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts.scalar_codec import decode_scalar
from core.domain import DeviceId
from data_generator.application.model.inputs import WEATHER_INPUTS, writable_inputs
from data_generator.application.runtime import GenerationState, Runtime
from data_generator.domain.model import GeneratorError, Scalar
from data_generator.infrastructure.diagnostics import Diagnostics
from data_generator.infrastructure.metrics import snapshot as metric_snapshot
from data_generator.infrastructure.snapshots import FileContent, SnapshotManager, file_sha256


async def _authorize_variables(context: Any, runtime: Runtime, permission: str) -> str:
    """Only a verified client-certificate common name can update model inputs."""
    security = runtime.settings.service.get("security", {})
    if not isinstance(security, dict) or not security.get("tls"):
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "mTLS is required")
        raise RuntimeError("gRPC abort returned")
    names = context.auth_context().get("x509_common_name", ())
    principal = names[0].decode("utf-8") if names else ""
    permissions = security.get("permissions", {})
    allowed = permissions.get(principal, []) if isinstance(permissions, dict) else []
    if not principal or permission not in allowed:
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, "caller lacks model permission")
        raise RuntimeError("gRPC abort returned")
    return principal


def runtime_error_code(error: GeneratorError) -> str:
    """Stable RPC classification without changing the frozen protobuf schema."""
    detail = str(error).lower()
    if "revision" in detail or "version unavailable" in detail:
        return "REVISION_CONFLICT"
    if "not found" in detail:
        return "NOT_FOUND"
    if "ownership" in detail or "unsettled" in detail or "conflict" in detail:
        return "RESOURCE_BUSY"
    if (
        "expected writable" in detail
        or "type mismatch" in detail
        or "missing targets" in detail
        or "repeated target" in detail
    ):
        return "TARGET_VALIDATION_FAILED"
    if (
        "preflight" in detail
        or "catalog page limit" in detail
        or "lease acquisition unavailable" in detail
    ):
        return "GATEWAY_UNAVAILABLE"
    return "INVALID_STATE"


def config_error_code(error: Exception) -> str:
    """Preserve actionable configuration failure categories in RPC responses."""
    detail = str(error).lower()
    if "revision" in detail or "candidate changed" in detail:
        return "REVISION_CONFLICT"
    if "only update owned" in detail or "outside data generator" in detail or "read-only" in detail:
        return "OWNERSHIP_DENIED"
    if "path" in detail or "escapes" in detail:
        return "INVALID_PATH"
    if "transaction" in detail or "recovery" in detail:
        return "FILE_COMMIT_FAILED"
    return "CONFIG_VALIDATION_FAILED"


def stamp(moment: datetime | None = None) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(moment or datetime.now(UTC))
    return result


def generation_proto(state: GenerationState) -> dg.Generation:
    result = dg.Generation(
        generation_id=state.generation_id,
        scenario_id=state.scenario.scenario_id,
        scenario_version=state.scenario.version,
        desired_state=state.desired_state,
        actual_state=state.actual_state,
        last_error=state.last_error,
    )
    if state.started_at is not None:
        result.started_at.CopyFrom(stamp(state.started_at))
    return result


class DataGeneratorService(dg_rpc.DataGeneratorServiceServicer):
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def _devices(self) -> dict[str, dg.Device]:
        devices: dict[str, dg.Device] = {}
        for scenario in self.runtime.settings.scenarios.values():
            for device_id in scenario.devices:
                devices[device_id] = dg.Device(
                    device_id=device_id,
                    protocol="virtual",
                    actual_state="STOPPED",
                )
        for state in self.runtime.generations.values():
            if state.actual_state in ("STARTING", "RUNNING", "STOPPING"):
                for device_id in state.scenario.devices:
                    if device_id not in devices:
                        # Existing Generation retains its original configuration binding.
                        devices[device_id] = dg.Device(
                            device_id=device_id,
                            protocol="virtual",
                        )
                    devices[device_id].generation_id = state.generation_id
                    devices[device_id].actual_state = state.actual_state
        return devices

    async def ListDevices(
        self,
        request: dg.ListDevicesRequest,
        context: Any,
    ) -> dg.ListDevicesResponse:
        filtered = [
            device
            for device_id, device in sorted(self._devices().items())
            if request.filter in device_id
        ]
        try:
            offset = int(request.page_token or "0")
            if offset < 0 or offset > len(filtered):
                raise ValueError("invalid offset")
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("gRPC abort returned") from None
        size = min(request.page_size or 100, 1000)
        end = offset + size
        return dg.ListDevicesResponse(
            devices=filtered[offset:end],
            next_page_token=str(end) if end < len(filtered) else "",
        )

    async def GetDevice(
        self,
        request: common.ObjectId,
        context: Any,
    ) -> dg.GetDeviceResponse:
        device = self._devices().get(request.id)
        if device is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "device not found")
            raise RuntimeError("gRPC abort returned")
        return dg.GetDeviceResponse(device=device)

    async def ListScenarios(
        self, request: dg.ListScenariosRequest, context: Any
    ) -> dg.ListScenariosResponse:
        scenarios = [
            dg.Scenario(
                scenario_id=s.scenario_id,
                active_version=s.version,
                available_versions=[s.version],
                device_ids=list(s.devices),
                description=s.description,
            )
            for s in sorted(
                self.runtime.settings.scenarios.values(), key=lambda item: item.scenario_id
            )
        ]
        try:
            offset = int(request.page_token or "0")
            if offset < 0 or offset > len(scenarios):
                raise ValueError("invalid offset")
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("unreachable") from None
        size = min(request.page_size or 100, 1000)
        end = offset + size
        return dg.ListScenariosResponse(
            scenarios=scenarios[offset:end],
            next_page_token=str(end) if end < len(scenarios) else "",
        )

    async def GetGeneration(
        self, request: common.ObjectId, context: Any
    ) -> dg.GetGenerationResponse:
        state = await self.runtime.get_generation(request.id)
        if state is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "generation not found")
            raise RuntimeError("unreachable")
        return dg.GetGenerationResponse(generation=generation_proto(state))

    async def StartGeneration(
        self, request: dg.StartGenerationRequest, context: Any
    ) -> dg.StartGenerationResponse:
        try:
            state = await self.runtime.start(
                request.scenario_id,
                request.scenario_version if request.HasField("scenario_version") else None,
                request.expected_revision,
            )
        except GeneratorError as exc:
            return dg.StartGenerationResponse(
                accepted=False,
                actual_state="REJECTED",
                error_code=runtime_error_code(exc),
                message=str(exc),
            )
        return dg.StartGenerationResponse(
            generation_id=state.generation_id, accepted=True, actual_state=state.actual_state
        )

    async def StopGeneration(
        self, request: dg.StopGenerationRequest, context: Any
    ) -> dg.StopGenerationResponse:
        try:
            state = await self.runtime.stop(request.generation_id, request.expected_revision)
        except GeneratorError as exc:
            return dg.StopGenerationResponse(
                accepted=False,
                actual_state="REJECTED",
                error_code=runtime_error_code(exc),
                message=str(exc),
            )
        return dg.StopGenerationResponse(accepted=True, actual_state=state.actual_state)

    async def ListVariables(
        self, request: dg.ListVariablesRequest, context: Any
    ) -> dg.ListVariablesResponse:
        await _authorize_variables(context, self.runtime, "variables_read")
        state = await self.runtime.get_generation(request.generation_id)
        if state is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "generation not found")
            raise RuntimeError("gRPC abort returned")
        if not request.device_id or request.device_id not in state.scenario.devices:
            await context.abort(grpc.StatusCode.NOT_FOUND, "device not in generation")
            raise RuntimeError("gRPC abort returned")
        shared = state.bound_settings.shared
        device = shared.site.devices[DeviceId(request.device_id)]
        model = shared.device_models[device.device_model_id]
        table = shared.point_tables[model.point_table_id]
        physical = state.physical_engine
        kind = physical.kinds.get(request.device_id, "") if physical else ""
        writable = writable_inputs(kind)
        ppc_owned = (
            physical is not None and physical.controller_modes.get(request.device_id) == "ppc"
        )
        entries: list[dg.ModelVariable] = []
        for point_id, point in table.points.items():
            business = shared.business_points[point.business_point_id]
            spec = writable.get(point_id)
            can_write = (
                spec is not None
                and state.actual_state == "RUNNING"
                and not (
                    ppc_owned and point_id in {"active_power_setpoint", "reactive_power_setpoint"}
                )
            )
            item = dg.ModelVariable(
                point_id=point_id,
                business_point_id=str(point.business_point_id),
                data_type=business.data_type.value,
                unit=point.source_unit.unit_id,
                role="INPUT" if spec is not None else "OUTPUT",
                writable=can_write,
                description=point.description or "",
                source="POINT_TABLE",
            )
            if spec is not None:
                if spec.minimum is not None:
                    item.minimum = spec.minimum
                if spec.maximum is not None:
                    item.maximum = spec.maximum
            entries.append(item)
        if physical and request.device_id in physical.weather_refs:
            for name, spec in WEATHER_INPUTS.items():
                item = dg.ModelVariable(
                    point_id=name,
                    data_type=spec.data_type,
                    unit=spec.unit,
                    role="INPUT",
                    writable=state.actual_state == "RUNNING",
                    source="WEATHER_INPUT",
                    description=spec.description,
                )
                if spec.minimum is not None:
                    item.minimum = spec.minimum
                if spec.maximum is not None:
                    item.maximum = spec.maximum
                entries.append(item)
        entries.sort(key=lambda item: item.point_id)
        digest = hashlib.sha256(
            f"{request.generation_id}:{request.device_id}:{state.bound_settings.revision}".encode()
        ).hexdigest()[:20]
        try:
            if request.page_token:
                version, offset_text = request.page_token.split(":", 1)
                if version != digest:
                    raise ValueError("stale page token")
                offset = int(offset_text)
            else:
                offset = 0
            if offset < 0 or offset > len(entries):
                raise ValueError("invalid page offset")
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("gRPC abort returned") from None
        limit = min(request.page_size or 100, 500)
        end = min(offset + limit, len(entries))
        return dg.ListVariablesResponse(
            variables=entries[offset:end],
            next_page_token=f"{digest}:{end}" if end < len(entries) else "",
            point_table_version=str(model.point_table_id).rsplit("@", 1)[-1],
            generation_state=state.actual_state,
        )

    async def UpdateVariables(
        self, request: dg.UpdateVariablesRequest, context: Any
    ) -> dg.UpdateVariablesResponse:
        caller = await _authorize_variables(context, self.runtime, "variables_update")
        if not request.generation_id or not request.device_id or not request.request_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "required input identity missing")
            raise RuntimeError("gRPC abort returned")
        if not request.values or len(request.values) > 100:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid input batch size")
            raise RuntimeError("gRPC abort returned")
        values: dict[str, Scalar] = {}
        for item in request.values:
            if not item.point_id or item.point_id in values or not item.HasField("value"):
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT, "invalid or duplicate point ID"
                )
                raise RuntimeError("gRPC abort returned")
            if item.value.WhichOneof("kind") is None:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "scalar value absent")
                raise RuntimeError("gRPC abort returned")
            try:
                value = decode_scalar(item.value)
            except ValueError:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid scalar value")
                raise RuntimeError("gRPC abort returned") from None
            if value is None:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "null input not allowed")
                raise RuntimeError("gRPC abort returned")
            values[item.point_id] = value
        fingerprint = hashlib.sha256(request.SerializeToString(deterministic=True)).digest()
        try:
            step = await self.runtime.update_inputs(
                request.generation_id,
                request.device_id,
                caller,
                request.request_id,
                fingerprint,
                values,
            )
        except GeneratorError as exc:
            if "request_id already" in str(exc):
                await context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
                raise RuntimeError("gRPC abort returned") from None
            return dg.UpdateVariablesResponse(
                accepted=False,
                error_code="INPUT_REJECTED",
                message=str(exc),
                request_id=request.request_id,
            )
        return dg.UpdateVariablesResponse(
            accepted=True,
            effective_step=step,
            request_id=request.request_id,
        )

    async def GetInputExecution(
        self, request: dg.GetInputExecutionRequest, context: Any
    ) -> dg.GetInputExecutionResponse:
        caller = await _authorize_variables(context, self.runtime, "variables_update")
        if not request.generation_id.strip() or not request.request_id.strip():
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "request identity required")
            raise RuntimeError("gRPC abort returned")
        state, step = await self.runtime.get_input_execution(
            request.generation_id, caller, request.request_id
        )
        return dg.GetInputExecutionResponse(
            request_id=request.request_id, state=state, effective_step=step
        )


class CommonService(common_rpc.CommonServiceServicer):
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.snapshots = SnapshotManager(runtime.settings)
        self.diagnostics = Diagnostics(runtime.settings)
        options = runtime.settings.service.get("diagnose", {})
        self._diagnostics_permits = asyncio.Semaphore(int(options.get("max_concurrent", 2)))
        self._diagnostics_wait = float(options.get("permit_wait_seconds", 0.5))
        self.started = datetime.now(UTC)
        self._config_lock = asyncio.Lock()

    async def GetServiceInfo(
        self,
        request: common.Empty,
        context: Any,
    ) -> common.GetServiceInfoResponse:
        return common.GetServiceInfoResponse(
            service_id="data_generator",
            process_role="generator",
            software_version="0.1.0",
            contract_version="v1",
            started_at=stamp(self.started),
        )

    async def GetServiceStatus(
        self,
        request: common.Empty,
        context: Any,
    ) -> common.GetServiceStatusResponse:
        failed = any(state.actual_state == "FAILED" for state in self.runtime.generations.values())
        unsafe = self.runtime.writer.unsafe_gateways
        gateways = {
            gateway_id
            for scenario in self.runtime.settings.scenarios.values()
            for gateway_id in scenario.gateways
        }
        return common.GetServiceStatusResponse(
            health="DEGRADED" if failed or unsafe else "HEALTHY",
            dependencies=[
                common.DependencyHealth(
                    name=f"gateway:{gateway_id}",
                    health="DEGRADED" if gateway_id in unsafe else "UNKNOWN",
                    detail="unsettled output" if gateway_id in unsafe else "no active health probe",
                )
                for gateway_id in sorted(gateways)
            ],
            observed_at=stamp(),
        )

    async def GetCapabilities(
        self,
        request: common.Empty,
        context: Any,
    ) -> common.GetCapabilitiesResponse:
        return common.GetCapabilitiesResponse(
            rpc_names=[
                m.name
                for m in dg.DESCRIPTOR.services_by_name["DataGeneratorService"].methods
                if (
                    m.name not in {"ListVariables", "UpdateVariables", "GetInputExecution"}
                    or bool(self.runtime.settings.service.get("security", {}).get("tls"))
                )
            ]
            + [m.name for m in common.DESCRIPTOR.services_by_name["CommonService"].methods],
            protocols=["grpc"],
            features=["simulation", "replay_csv", "gateway_update"],
        )

    async def GetConfigSnapshot(
        self,
        request: common.GetConfigSnapshotRequest,
        context: Any,
    ) -> common.GetConfigSnapshotResponse:
        scope = common.SnapshotScope.Name(request.scope).removeprefix("SNAPSHOT_SCOPE_")
        async with self._config_lock:
            try:
                files = await asyncio.to_thread(
                    self.snapshots.get,
                    scope,
                    request.candidate_id,
                )
            except (GeneratorError, OSError, ValueError) as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                raise RuntimeError("gRPC abort returned") from exc
            if scope == "RUNTIME":
                revision = self.runtime.settings.revision
            elif scope == "CANDIDATE":
                revision = self.snapshots.candidate_revision(request.candidate_id)
            else:
                revision = await asyncio.to_thread(self.snapshots.disk_revision)
            snapshot_id = request.candidate_id if scope == "CANDIDATE" else revision
        return common.GetConfigSnapshotResponse(
            snapshot_id=snapshot_id,
            revision=revision,
            files=[
                common.SnapshotFile(
                    relative_path=item.path,
                    content=item.data,
                    size_bytes=len(item.data),
                    sha256=file_sha256(item.data),
                )
                for item in files
            ],
            apply_results=[
                common.ApplicationResult(
                    object_id=state.generation_id,
                    status="BOUND_OLD_VERSION",
                    message=(
                        f"{state.scenario.scenario_id}@{state.scenario.version}"
                        " retained by active Generation"
                    ),
                )
                for state in self.runtime.generations.values()
                if scope == "RUNTIME"
                and state.actual_state in ("STARTING", "RUNNING", "STOPPING")
                and (
                    state.scenario
                    != self.runtime.settings.scenarios.get(state.scenario.scenario_id)
                )
            ],
        )

    async def PushConfigSnapshot(
        self,
        request: common.PushConfigSnapshotRequest,
        context: Any,
    ) -> common.PushConfigSnapshotResponse:
        # The transaction owns the lock after client cancellation.
        return await asyncio.shield(asyncio.create_task(self._push_snapshot(request)))

    async def _push_snapshot(
        self,
        request: common.PushConfigSnapshotRequest,
    ) -> common.PushConfigSnapshotResponse:
        async with self._config_lock:
            try:
                files = tuple(FileContent(f.relative_path, bytes(f.content)) for f in request.files)
                for original, item in zip(request.files, files, strict=False):
                    if original.size_bytes != len(item.data):
                        raise GeneratorError("snapshot size mismatch")
                    if original.sha256 and original.sha256 != file_sha256(item.data):
                        raise GeneratorError("snapshot SHA-256 mismatch")
                candidate = await asyncio.to_thread(
                    self.snapshots.push,
                    files,
                    request.expected_revision,
                )
            except (GeneratorError, OSError, ValueError) as exc:
                return common.PushConfigSnapshotResponse(
                    valid=False,
                    validation=[
                        common.ValidationIssue(
                            path="snapshot",
                            code=config_error_code(exc),
                            message=str(exc),
                        )
                    ],
                    base_revision=self.snapshots.settings.revision,
                )
        return common.PushConfigSnapshotResponse(
            candidate_id=candidate,
            valid=True,
            base_revision=self.snapshots.settings.revision,
        )

    async def ApplyConfigSnapshot(
        self,
        request: common.ApplyConfigSnapshotRequest,
        context: Any,
    ) -> common.ApplyConfigSnapshotResponse:
        # Cancellation of the caller cannot release a live file-commit lock.
        return await asyncio.shield(asyncio.create_task(self._apply_snapshot(request)))

    async def _apply_snapshot(
        self,
        request: common.ApplyConfigSnapshotRequest,
    ) -> common.ApplyConfigSnapshotResponse:
        async with self._config_lock:
            try:
                settings, restart_required = await self.runtime.commit_configuration(
                    lambda: self.snapshots.apply(
                        request.candidate_id,
                        request.expected_revision,
                    ),
                )
            except (GeneratorError, OSError, ValueError) as exc:
                self.runtime.record("ERROR", "config", "APPLY_FAILED", str(exc))
                return common.ApplyConfigSnapshotResponse(
                    file_commit="FAILED",
                    apply_results=[
                        common.ApplicationResult(
                            object_id="config",
                            status="FAILED",
                            error_code=config_error_code(exc),
                            message=str(exc),
                        )
                    ],
                    effective_revision=self.snapshots.settings.revision,
                )
            self.diagnostics = Diagnostics(self.runtime.settings)
            self.runtime.record("INFO", "config", "APPLIED", settings.revision)
            return common.ApplyConfigSnapshotResponse(
                file_commit="COMMITTED",
                effective_revision=self.runtime.settings.revision,
                restart_required=restart_required,
                application_id=request.candidate_id,
                apply_results=[
                    common.ApplicationResult(
                        object_id=scenario_id,
                        message=(
                            f"disk_revision={settings.revision}; "
                            f"effective_revision={self.runtime.settings.revision}"
                        ),
                        status=(
                            "RESTART_REQUIRED"
                            if restart_required
                            else "BOUND_OLD_VERSION"
                            if any(
                                state.scenario.scenario_id == scenario_id
                                and state.actual_state in ("STARTING", "RUNNING", "STOPPING")
                                for state in self.runtime.generations.values()
                            )
                            else "APPLIED"
                        ),
                    )
                    for scenario_id in settings.scenarios
                ],
            )

    async def GetMetrics(
        self,
        request: common.GetMetricsRequest,
        context: Any,
    ) -> common.GetMetricsResponse:
        try:
            return metric_snapshot(self.runtime, request.scope, request.window_ms)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("gRPC abort returned") from exc

    async def GetEvents(
        self, request: common.GetEventsRequest, context: Any
    ) -> common.GetEventsResponse:
        try:
            offset = int(request.page_token or "0")
            if offset < 0:
                raise ValueError("negative offset")
            if (
                request.HasField("from_time")
                and request.HasField("to_time")
                and request.from_time.ToDatetime(tzinfo=UTC)
                > request.to_time.ToDatetime(tzinfo=UTC)
            ):
                raise ValueError("invalid event time range")
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("unreachable") from None
        events = list(self.runtime.events)
        if request.HasField("from_time"):
            start = request.from_time.ToDatetime(tzinfo=UTC)
            events = [event for event in events if event.timestamp >= start]
        if request.HasField("to_time"):
            end = request.to_time.ToDatetime(tzinfo=UTC)
            events = [event for event in events if event.timestamp < end]
        if request.filter:
            events = [
                event
                for event in events
                if request.filter in event.source or request.filter in event.error_code
            ]
        if offset > len(events):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "event page out of range")
            raise RuntimeError("unreachable")
        limit = min(request.page_size or 100, 1000)
        selection = events[offset : offset + limit]
        return common.GetEventsResponse(
            events=[
                common.Event(
                    event_id=e.event_id,
                    timestamp=stamp(e.timestamp),
                    severity=e.severity,
                    source=e.source,
                    error_code=e.error_code,
                    message=e.message,
                )
                for e in selection
            ],
            next_page_token=str(offset + limit) if offset + limit < len(events) else "",
        )

    async def Diagnose(
        self,
        request: common.DiagnoseRequest,
        context: Any,
    ) -> common.DiagnoseResponse:
        try:
            await asyncio.wait_for(
                self._diagnostics_permits.acquire(),
                timeout=self._diagnostics_wait,
            )
        except TimeoutError:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "diagnostics capacity reached")
            raise RuntimeError("gRPC abort returned") from None
        self.runtime.counters["diagnostic_total"] = (
            self.runtime.counters.get("diagnostic_total", 0) + 1
        )
        try:
            response = await self.diagnostics.run(request)
            if response.overall_status == "FAIL":
                self.runtime.counters["diagnostic_failed"] = (
                    self.runtime.counters.get("diagnostic_failed", 0) + 1
                )
            if "NO_DIAGNOSTIC_TARGETS" in response.limitations:
                self.runtime.counters["diagnostic_no_targets"] = (
                    self.runtime.counters.get("diagnostic_no_targets", 0) + 1
                )
            self.runtime.record(
                "WARNING" if response.overall_status != "PASS" else "INFO",
                request.target_id or "diagnostics",
                f"DIAGNOSTIC_{response.overall_status}",
                request.correlation_id,
            )
            return response
        finally:
            self._diagnostics_permits.release()


def validate_rpc_contracts() -> None:
    """Fail startup on missing public unary implementations or descriptor drift."""
    services = (
        (dg.DESCRIPTOR.services_by_name["DataGeneratorService"], DataGeneratorService, 9),
        (common.DESCRIPTOR.services_by_name["CommonService"], CommonService, 9),
    )
    for descriptor, implementation, expected_count in services:
        if len(descriptor.methods) != expected_count:
            raise GeneratorError(f"{descriptor.full_name}: unexpected RPC count")
        for method in descriptor.methods:
            if method.client_streaming or method.server_streaming:
                raise GeneratorError(f"{descriptor.full_name}.{method.name}: not unary")
            if method.name not in implementation.__dict__:
                raise GeneratorError(
                    f"{descriptor.full_name}.{method.name}: implementation missing"
                )


def make_server(runtime: Runtime, bind: str) -> grpc.aio.Server:
    validate_rpc_contracts()
    security = runtime.settings.service.get("security")
    if security is not None and (
        not isinstance(security, dict) or set(security) != {"tls", "permissions"}
    ):
        raise GeneratorError("security must declare tls and permissions")
    tls = security.get("tls") if isinstance(security, dict) else None
    if tls is not None and (
        not isinstance(tls, dict) or set(tls) != {"cert_file", "key_file", "ca_file"}
    ):
        raise GeneratorError("tls must declare cert_file, key_file and ca_file")
    permissions = security.get("permissions") if isinstance(security, dict) else {}
    if not isinstance(permissions, dict):
        raise GeneratorError("security permissions must be a mapping")
    supported = {"variables_read", "variables_update"}
    if any(
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(rights, list)
        or not rights
        or any(not isinstance(right, str) or right not in supported for right in rights)
        for name, rights in permissions.items()
    ):
        raise GeneratorError("invalid Data Generator caller permission")
    if bool(tls) != bool(permissions):
        raise GeneratorError("mTLS and explicit caller permissions are both required")
    if not tls and not bind.startswith(("127.0.0.1:", "[::1]:")):
        raise GeneratorError("insecure gRPC listener restricted to loopback")
    server = grpc.aio.server()
    dg_rpc.add_DataGeneratorServiceServicer_to_server(DataGeneratorService(runtime), server)
    common_rpc.add_CommonServiceServicer_to_server(CommonService(runtime), server)
    if tls:
        files = {}
        for key in ("cert_file", "key_file", "ca_file"):
            value = tls.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise GeneratorError(f"security.tls.{key} must be an absolute path")
            files[key] = Path(value).read_bytes()
        credentials = grpc.ssl_server_credentials(
            [(files["key_file"], files["cert_file"])],
            root_certificates=files["ca_file"],
            require_client_auth=True,
        )
        bound = server.add_secure_port(bind, credentials)
    else:
        bound = server.add_insecure_port(bind)
    if bound == 0:
        raise GeneratorError("unable to bind gRPC listener")
    return server
