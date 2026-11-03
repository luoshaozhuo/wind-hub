"""Public Data Generator and Common unary gRPC services."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from data_generator.application.runtime import GenerationState, Runtime
from data_generator.application.snapshots import FileContent, SnapshotManager, file_sha256
from data_generator.domain.model import GeneratorError


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

    async def ListDevices(self, request: dg.ListDevicesRequest, context: Any) -> dg.ListDevicesResponse:
        items = {}
        for scenario in self.runtime.settings.scenarios.values():
            for device_id in scenario.devices:
                items[device_id] = dg.Device(
                    device_id=device_id, protocol="virtual",
                    actual_state="STOPPED",
                )
        for generation in self.runtime.generations.values():
            if generation.actual_state in ("STARTING", "RUNNING", "STOPPING"):
                for device_id in generation.scenario.devices:
                    items[device_id].generation_id = generation.generation_id
                    items[device_id].actual_state = generation.actual_state
        filtered = [item for key, item in sorted(items.items()) if request.filter in key]
        offset = int(request.page_token or "0")
        if offset < 0 or offset > len(filtered):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid page token")
        size = min(request.page_size or 100, 1000)
        end = offset + size
        return dg.ListDevicesResponse(devices=filtered[offset:end],
                                      next_page_token=str(end) if end < len(filtered) else "")

    async def GetDevice(self, request: common.ObjectId, context: Any) -> dg.GetDeviceResponse:
        result = await self.ListDevices(dg.ListDevicesRequest(), context)
        for device in result.devices:
            if device.device_id == request.id:
                return dg.GetDeviceResponse(device=device)
        await context.abort(grpc.StatusCode.NOT_FOUND, "device not found")
        raise RuntimeError("unreachable")

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
        state = self.runtime.generations.get(request.id)
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
            return dg.StartGenerationResponse(accepted=False, actual_state="REJECTED",
                                              error_code="INVALID_STATE", message=str(exc))
        return dg.StartGenerationResponse(generation_id=state.generation_id,
                                          accepted=True, actual_state=state.actual_state)

    async def StopGeneration(self, request: dg.StopGenerationRequest,
                             context: Any) -> dg.StopGenerationResponse:
        try:
            state = await self.runtime.stop(request.generation_id, request.expected_revision)
        except GeneratorError as exc:
            return dg.StopGenerationResponse(accepted=False, actual_state="REJECTED",
                                             error_code="INVALID_STATE", message=str(exc))
        return dg.StopGenerationResponse(accepted=True, actual_state=state.actual_state)


class CommonService(common_rpc.CommonServiceServicer):
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.snapshots = SnapshotManager(runtime.settings)
        self.started = datetime.now(timezone.utc)

    async def GetServiceInfo(self, request: common.Empty, context: Any) -> common.GetServiceInfoResponse:
        return common.GetServiceInfoResponse(service_id="data_generator", process_role="generator",
                                             software_version="0.1.0", contract_version="v1",
                                             started_at=stamp(self.started))

    async def GetServiceStatus(self, request: common.Empty, context: Any) -> common.GetServiceStatusResponse:
        return common.GetServiceStatusResponse(health="HEALTHY",
                                               observed_at=stamp())

    async def GetCapabilities(self, request: common.Empty, context: Any) -> common.GetCapabilitiesResponse:
        return common.GetCapabilitiesResponse(
            rpc_names=[m.name for m in dg.DESCRIPTOR.services_by_name[
                "DataGeneratorService"].methods]
                + [m.name for m in common.DESCRIPTOR.services_by_name["CommonService"].methods],
            protocols=["grpc"], features=["simulation", "replay_csv", "gateway_update"],
        )

    async def GetConfigSnapshot(self, request: common.GetConfigSnapshotRequest,
                                context: Any) -> common.GetConfigSnapshotResponse:
        name = common.SnapshotScope.Name(request.scope).removeprefix("SNAPSHOT_SCOPE_")
        try:
            files = self.snapshots.get(name, request.candidate_id)
        except GeneratorError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            raise RuntimeError("unreachable") from exc
        return common.GetConfigSnapshotResponse(
            revision=self.runtime.settings.revision,
            files=[common.SnapshotFile(relative_path=f.path, content=f.data,
                    size_bytes=len(f.data), sha256=file_sha256(f.data)) for f in files],
        )

    async def PushConfigSnapshot(self, request: common.PushConfigSnapshotRequest,
                                 context: Any) -> common.PushConfigSnapshotResponse:
        try:
            candidate = self.snapshots.push(
                tuple(FileContent(f.relative_path, bytes(f.content)) for f in request.files),
                request.expected_revision,
            )
        except GeneratorError as exc:
            return common.PushConfigSnapshotResponse(
                valid=False, validation=[common.ValidationIssue(
                    path="snapshot", code="INVALID", message=str(exc))],
                base_revision=self.runtime.settings.revision,
            )
        return common.PushConfigSnapshotResponse(candidate_id=candidate, valid=True,
                                                 base_revision=self.runtime.settings.revision)

    async def ApplyConfigSnapshot(self, request: common.ApplyConfigSnapshotRequest,
                                  context: Any) -> common.ApplyConfigSnapshotResponse:
        try:
            self.snapshots.apply(request.candidate_id, request.expected_revision)
        except GeneratorError as exc:
            return common.ApplyConfigSnapshotResponse(
                file_commit="FAILED",
                apply_results=[common.ApplicationResult(object_id="config",
                              status="FAILED", error_code="APPLY_FAILED", message=str(exc))],
                effective_revision=self.runtime.settings.revision,
            )
        raise RuntimeError("unreachable")

    async def GetMetrics(self, request: common.GetMetricsRequest,
                         context: Any) -> common.GetMetricsResponse:
        if request.scope not in ("", "service"):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid metrics scope")
        return common.GetMetricsResponse(metrics=[
            common.Metric(name=key, value=float(value), unit="count", window_ms=0,
                          sampled_at=stamp()) for key, value in self.runtime.counters.items()
        ])

    async def GetEvents(self, request: common.GetEventsRequest,
                        context: Any) -> common.GetEventsResponse:
        return common.GetEventsResponse()

    async def Diagnose(self, request: common.DiagnoseRequest,
                       context: Any) -> common.DiagnoseResponse:
        stages = []
        for name in (request.stages or ("source", "mapping", "network", "grpc", "write")):
            if name in ("source", "mapping"):
                status, code = "PASS", ""
            else:
                status, code = "SKIPPED", "NOT_VERIFIED"
            stages.append(common.DiagnosticStage(
                stage=name, status=status, error_code=code,
                source="data_generator", observed_at=stamp(),
            ))
        return common.DiagnoseResponse(overall_status="UNKNOWN", stages=stages,
                                       limitations=["Gateway data delivery not actively verified"])


def make_server(runtime: Runtime, bind: str) -> grpc.aio.Server:
    if not bind.startswith(("127.0.0.1:", "[::1]:")):
        raise GeneratorError("insecure gRPC listener restricted to loopback")
    server = grpc.aio.server()
    dg_rpc.add_DataGeneratorServiceServicer_to_server(DataGeneratorService(runtime), server)
    common_rpc.add_CommonServiceServicer_to_server(CommonService(runtime), server)
    if server.add_insecure_port(bind) == 0:
        raise GeneratorError("unable to bind gRPC listener")
    return server
