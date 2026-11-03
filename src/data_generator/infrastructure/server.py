"""Public Data Generator and Common unary gRPC services."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from data_generator.application.runtime import GenerationState, Runtime
from data_generator.infrastructure.diagnostics import Diagnostics
from data_generator.infrastructure.metrics import snapshot as metric_snapshot
from data_generator.infrastructure.snapshots import FileContent, SnapshotManager, file_sha256
from data_generator.domain.model import GeneratorError


def runtime_error_code(error: GeneratorError) -> str:
    """Stable RPC classification without changing the frozen protobuf schema."""
    detail = str(error).lower()
    if "revision" in detail or "version unavailable" in detail:
        return "REVISION_CONFLICT"
    if "not found" in detail:
        return "NOT_FOUND"
    if "ownership" in detail or "unsettled" in detail or "conflict" in detail:
        return "RESOURCE_BUSY"
    if "preflight" in detail or "missing targets" in detail:
        return "GATEWAY_UNAVAILABLE"
    return "INVALID_STATE"


def config_error_code(error: Exception) -> str:
    """Preserve actionable configuration failure categories in RPC responses."""
    detail = str(error).lower()
    if "revision" in detail or "candidate changed" in detail:
        return "REVISION_CONFLICT"
    if "only update owned" in detail or "outside data generator" in detail:
        return "OWNERSHIP_DENIED"
    if "path" in detail or "escapes" in detail:
        return "INVALID_PATH"
    if "transaction" in detail or "recovery" in detail:
        return "FILE_COMMIT_FAILED"
    return "CONFIG_VALIDATION_FAILED"


def stamp(moment: datetime | None = None) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(moment or datetime.now(timezone.utc))
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
                    device_id=device_id, protocol="virtual", actual_state="STOPPED",
                )
        for state in self.runtime.generations.values():
            if state.actual_state in ("STARTING", "RUNNING", "STOPPING"):
                for device_id in state.scenario.devices:
                    if device_id not in devices:
                        # Existing Generation retains its original configuration binding.
                        devices[device_id] = dg.Device(
                            device_id=device_id, protocol="virtual",
                        )
                    devices[device_id].generation_id = state.generation_id
                    devices[device_id].actual_state = state.actual_state
        return devices

    async def ListDevices(
        self, request: dg.ListDevicesRequest, context: Any,
    ) -> dg.ListDevicesResponse:
        filtered = [
            device for device_id, device in sorted(self._devices().items())
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
        self, request: common.ObjectId, context: Any,
    ) -> dg.GetDeviceResponse:
        device = self._devices().get(request.id)
        if device is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "device not found")
            raise RuntimeError("gRPC abort returned")
        return dg.GetDeviceResponse(device=device)

    async def ListScenarios(self, request: dg.ListScenariosRequest,
                            context: Any) -> dg.ListScenariosResponse:
        scenarios = [dg.Scenario(
            scenario_id=s.scenario_id, active_version=s.version,
            available_versions=[s.version], device_ids=list(s.devices),
            description=s.description,
        ) for s in sorted(self.runtime.settings.scenarios.values(),
                          key=lambda item: item.scenario_id)]
        try:
            offset = int(request.page_token or "0")
            if offset < 0 or offset > len(scenarios):
                raise ValueError("invalid offset")
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("unreachable") from None
        size = min(request.page_size or 100, 1000)
        end = offset + size
        return dg.ListScenariosResponse(scenarios=scenarios[offset:end],
                                        next_page_token=str(end) if end < len(scenarios) else "")

    async def GetGeneration(self, request: common.ObjectId,
                            context: Any) -> dg.GetGenerationResponse:
        state = await self.runtime.get_generation(request.id)
        if state is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "generation not found")
            raise RuntimeError("unreachable")
        return dg.GetGenerationResponse(generation=generation_proto(state))

    async def StartGeneration(self, request: dg.StartGenerationRequest,
                              context: Any) -> dg.StartGenerationResponse:
        try:
            state = await self.runtime.start(
                request.scenario_id,
                request.scenario_version if request.HasField("scenario_version") else None,
                request.expected_revision,
            )
        except GeneratorError as exc:
            return dg.StartGenerationResponse(
                accepted=False, actual_state="REJECTED",
                error_code=runtime_error_code(exc), message=str(exc),
            )
        return dg.StartGenerationResponse(generation_id=state.generation_id,
                                          accepted=True, actual_state=state.actual_state)

    async def StopGeneration(self, request: dg.StopGenerationRequest,
                             context: Any) -> dg.StopGenerationResponse:
        try:
            state = await self.runtime.stop(request.generation_id, request.expected_revision)
        except GeneratorError as exc:
            return dg.StopGenerationResponse(
                accepted=False, actual_state="REJECTED",
                error_code=runtime_error_code(exc), message=str(exc),
            )
        return dg.StopGenerationResponse(accepted=True, actual_state=state.actual_state)


class CommonService(common_rpc.CommonServiceServicer):
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.snapshots = SnapshotManager(runtime.settings)
        self.diagnostics = Diagnostics(runtime.settings)
        limit = int(runtime.settings.service.get("diagnose", {}).get("max_concurrent", 2))
        self._diagnostics_permits = asyncio.Semaphore(limit)
        self.started = datetime.now(timezone.utc)
        self._config_lock = asyncio.Lock()

    async def GetServiceInfo(self, request: common.Empty, context: Any) -> common.GetServiceInfoResponse:
        return common.GetServiceInfoResponse(service_id="data_generator", process_role="generator",
                                             software_version="0.1.0", contract_version="v1",
                                             started_at=stamp(self.started))

    async def GetServiceStatus(self, request: common.Empty, context: Any) -> common.GetServiceStatusResponse:
        failed = any(
            state.actual_state == "FAILED"
            for state in self.runtime.generations.values()
        )
        unsafe = self.runtime.writer.unsafe_gateways
        gateways = {
            gateway_id for scenario in self.runtime.settings.scenarios.values()
            for gateway_id in scenario.gateways
        }
        return common.GetServiceStatusResponse(
            health="DEGRADED" if failed or unsafe else "HEALTHY",
            dependencies=[
                common.DependencyHealth(
                    name=f"gateway:{gateway_id}",
                    health="DEGRADED" if gateway_id in unsafe else "UNKNOWN",
                    detail="unsettled output" if gateway_id in unsafe
                    else "no active health probe",
                )
                for gateway_id in sorted(gateways)
            ],
            observed_at=stamp(),
        )

    async def GetCapabilities(self, request: common.Empty, context: Any) -> common.GetCapabilitiesResponse:
        return common.GetCapabilitiesResponse(
            rpc_names=[m.name for m in dg.DESCRIPTOR.services_by_name[
                "DataGeneratorService"].methods]
                + [m.name for m in common.DESCRIPTOR.services_by_name["CommonService"].methods],
            protocols=["grpc"], features=["simulation", "replay_csv", "gateway_update"],
        )

    async def GetConfigSnapshot(
        self, request: common.GetConfigSnapshotRequest, context: Any,
    ) -> common.GetConfigSnapshotResponse:
        scope = common.SnapshotScope.Name(request.scope).removeprefix("SNAPSHOT_SCOPE_")
        async with self._config_lock:
            try:
                files = await asyncio.to_thread(
                    self.snapshots.get, scope, request.candidate_id,
                )
            except (GeneratorError, OSError, ValueError) as exc:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                raise RuntimeError("gRPC abort returned") from exc
            revision = (
                self.runtime.settings.revision if scope == "RUNTIME"
                else await asyncio.to_thread(self.snapshots.disk_revision)
            )
            snapshot_id = (
                request.candidate_id if scope == "CANDIDATE" else revision
            )
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
                    state.scenario != self.runtime.settings.scenarios.get(
                        state.scenario.scenario_id
                    )
                )
            ],
        )

    async def PushConfigSnapshot(
        self, request: common.PushConfigSnapshotRequest, context: Any,
    ) -> common.PushConfigSnapshotResponse:
        # The transaction owns the lock after client cancellation.
        return await asyncio.shield(
            asyncio.create_task(self._push_snapshot(request))
        )

    async def _push_snapshot(
        self, request: common.PushConfigSnapshotRequest,
    ) -> common.PushConfigSnapshotResponse:
        async with self._config_lock:
            try:
                files = tuple(FileContent(f.relative_path, bytes(f.content))
                              for f in request.files)
                for original, item in zip(request.files, files):
                    if original.size_bytes != len(item.data):
                        raise GeneratorError("snapshot size mismatch")
                    if original.sha256 and original.sha256 != file_sha256(item.data):
                        raise GeneratorError("snapshot SHA-256 mismatch")
                candidate = await asyncio.to_thread(
                    self.snapshots.push, files, request.expected_revision,
                )
            except (GeneratorError, OSError, ValueError) as exc:
                return common.PushConfigSnapshotResponse(
                    valid=False, validation=[common.ValidationIssue(
                        path="snapshot", code=config_error_code(exc), message=str(exc),
                    )],
                    base_revision=self.snapshots.settings.revision,
                )
        return common.PushConfigSnapshotResponse(
            candidate_id=candidate,
            valid=True,
            base_revision=self.snapshots.settings.revision,
        )

    async def ApplyConfigSnapshot(
        self, request: common.ApplyConfigSnapshotRequest, context: Any,
    ) -> common.ApplyConfigSnapshotResponse:
        # Cancellation of the caller cannot release a live file-commit lock.
        return await asyncio.shield(
            asyncio.create_task(self._apply_snapshot(request))
        )

    async def _apply_snapshot(
        self, request: common.ApplyConfigSnapshotRequest,
    ) -> common.ApplyConfigSnapshotResponse:
        async with self._config_lock:
            try:
                settings, restart_required = await self.runtime.commit_configuration(
                    lambda: self.snapshots.apply(
                        request.candidate_id, request.expected_revision,
                    ),
                )
            except (GeneratorError, OSError, ValueError) as exc:
                self.runtime.record("ERROR", "config", "APPLY_FAILED", str(exc))
                return common.ApplyConfigSnapshotResponse(
                    file_commit="FAILED",
                    apply_results=[common.ApplicationResult(
                        object_id="config", status="FAILED",
                        error_code=config_error_code(exc), message=str(exc),
                    )],
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
                        status=(
                            "RESTART_REQUIRED" if restart_required
                            else "BOUND_OLD_VERSION" if any(
                                state.scenario.scenario_id == scenario_id
                                and state.actual_state in ("STARTING", "RUNNING", "STOPPING")
                                for state in self.runtime.generations.values()
                            ) else "APPLIED"
                        ),
                    )
                    for scenario_id in settings.scenarios
                ],
            )

    async def GetMetrics(
        self, request: common.GetMetricsRequest, context: Any,
    ) -> common.GetMetricsResponse:
        try:
            return metric_snapshot(self.runtime, request.scope, request.window_ms)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise RuntimeError("gRPC abort returned") from exc

    async def GetEvents(self, request: common.GetEventsRequest,
                        context: Any) -> common.GetEventsResponse:
        try:
            offset = int(request.page_token or "0")
            if offset < 0:
                raise ValueError("negative offset")
            if request.HasField("from_time") and request.HasField("to_time"):
                if request.from_time.ToDatetime(tzinfo=timezone.utc) > (
                    request.to_time.ToDatetime(tzinfo=timezone.utc)
                ):
                    raise ValueError("invalid event time range")
        except ValueError:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
            raise RuntimeError("unreachable") from None
        events = list(self.runtime.events)
        if request.HasField("from_time"):
            start = request.from_time.ToDatetime(tzinfo=timezone.utc)
            events = [event for event in events if event.timestamp >= start]
        if request.HasField("to_time"):
            end = request.to_time.ToDatetime(tzinfo=timezone.utc)
            events = [event for event in events if event.timestamp < end]
        if request.filter:
            events = [event for event in events if request.filter in event.source
                      or request.filter in event.error_code]
        if offset > len(events):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "event page out of range")
            raise RuntimeError("unreachable")
        limit = min(request.page_size or 100, 1000)
        selection = events[offset:offset + limit]
        return common.GetEventsResponse(
            events=[common.Event(
                event_id=e.event_id, timestamp=stamp(e.timestamp), severity=e.severity,
                source=e.source, error_code=e.error_code, message=e.message,
            ) for e in selection],
            next_page_token=str(offset + limit) if offset + limit < len(events) else "",
        )

    async def Diagnose(
        self, request: common.DiagnoseRequest, context: Any,
    ) -> common.DiagnoseResponse:
        try:
            await asyncio.wait_for(self._diagnostics_permits.acquire(), timeout=0.001)
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
        (dg.DESCRIPTOR.services_by_name["DataGeneratorService"], DataGeneratorService, 6),
        (common.DESCRIPTOR.services_by_name["CommonService"], CommonService, 9),
    )
    for descriptor, implementation, expected_count in services:
        if len(descriptor.methods) != expected_count:
            raise GeneratorError(
                f"{descriptor.full_name}: unexpected RPC count"
            )
        for method in descriptor.methods:
            if method.client_streaming or method.server_streaming:
                raise GeneratorError(f"{descriptor.full_name}.{method.name}: not unary")
            if method.name not in implementation.__dict__:
                raise GeneratorError(
                    f"{descriptor.full_name}.{method.name}: implementation missing"
                )


def make_server(runtime: Runtime, bind: str) -> grpc.aio.Server:
    validate_rpc_contracts()
    if not bind.startswith(("127.0.0.1:", "[::1]:")):
        raise GeneratorError("insecure gRPC listener restricted to loopback")
    server = grpc.aio.server()
    dg_rpc.add_DataGeneratorServiceServicer_to_server(DataGeneratorService(runtime), server)
    common_rpc.add_CommonServiceServicer_to_server(CommonService(runtime), server)
    if server.add_insecure_port(bind) == 0:
        raise GeneratorError("unable to bind gRPC listener")
    return server
