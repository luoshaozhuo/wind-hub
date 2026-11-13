"""Typed gRPC adapters for the frozen Common and Commander contracts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from time import perf_counter
from typing import Any

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from commander.application.service_lifecycle import CommanderService
from commander.domain.command import (
    TERMINAL_STATES,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
)
from core.configuration.effective import resolve_effective_device
from contracts import commander_pb2, commander_pb2_grpc, common_pb2, common_pb2_grpc
from core.application.errors import ConfigError
from core.domain import DeviceId, PointAccess


def _software_version() -> str:
    try:
        return version("wind-hub")
    except PackageNotFoundError:
        return "unknown"


def _timestamp(value: datetime) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(value.astimezone(UTC))
    return timestamp


def _page(page_size: int, token: str, count: int) -> tuple[int, int, str]:
    if page_size > 1000:
        raise ValueError("page_size must be <= 1000")
    if token:
        try:
            offset = int(token)
        except ValueError as exc:
            raise ValueError("invalid page_token") from exc
        if str(offset) != token or offset < 0 or offset > count:
            raise ValueError("invalid page_token")
    else:
        offset = 0
    limit = page_size or 100
    end = min(offset + limit, count)
    return offset, end, str(end) if end < count else ""


def _scalar(value: common_pb2.ScalarValue) -> bool | int | float | str:
    field = value.WhichOneof("kind")
    if field is None:
        raise ValueError("ScalarValue.kind is required")
    if field == "bool_value":
        return value.bool_value
    if field == "sint64_value":
        return value.sint64_value
    if field == "uint64_value":
        return value.uint64_value
    if field == "double_value":
        return value.double_value
    if field == "string_value":
        return value.string_value
    raise ValueError("bytes control is not supported by the Commander core")


class _Authorized:
    def __init__(
        self,
        service: CommanderService,
        permissions: dict[str, tuple[str, frozenset[str]]],
    ) -> None:
        self.service = service
        self.permissions = permissions

    async def require(self, context: grpc.aio.ServicerContext[Any, Any], scope: str) -> str:
        auth = context.auth_context()
        identities = auth.get("x509_subject_alternative_name", [])
        matched: set[str] = set()
        authorized: set[str] = set()
        for candidate in identities:
            identity = candidate.decode("utf-8", errors="strict")
            registered = self.permissions.get(identity)
            if registered is not None:
                caller_id, grants = registered
                matched.add(caller_id)
                if scope in grants:
                    authorized.add(caller_id)
        if len(matched) > 1:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "certificate identifies multiple configured callers",
            )
        if len(matched) == 1 and matched == authorized:
            return next(iter(matched))
        if identities:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "caller lacks permission")
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "client certificate identity missing")
        raise RuntimeError("unreachable")

    async def invalid(self, context: grpc.aio.ServicerContext[Any, Any], exc: Exception) -> None:
        if isinstance(exc, FileExistsError):
            code = grpc.StatusCode.ALREADY_EXISTS
        elif isinstance(exc, TimeoutError):
            code = grpc.StatusCode.DEADLINE_EXCEEDED
        elif isinstance(exc, OverflowError):
            code = grpc.StatusCode.RESOURCE_EXHAUSTED
        elif isinstance(exc, ValueError | TypeError | ConfigError):
            code = grpc.StatusCode.INVALID_ARGUMENT
        elif isinstance(exc, KeyError | FileNotFoundError):
            code = grpc.StatusCode.NOT_FOUND
        elif isinstance(exc, PermissionError):
            code = grpc.StatusCode.PERMISSION_DENIED
        elif isinstance(exc, RuntimeError) and "revision" in str(exc):
            code = grpc.StatusCode.ABORTED
        elif isinstance(exc, RuntimeError):
            code = grpc.StatusCode.FAILED_PRECONDITION
        else:
            code = grpc.StatusCode.INTERNAL
        await context.abort(code, str(exc))


class CommanderRpc(commander_pb2_grpc.CommanderServiceServicer):
    def __init__(self, boundary: _Authorized) -> None:
        self._boundary = boundary

    def _devices(self) -> list[commander_pb2.Device]:
        config = self._boundary.service.config_manager
        snapshot = config.snapshot
        devices: list[commander_pb2.Device] = []
        for device_id, device in sorted(snapshot.site.devices.items()):
            model = snapshot.device_models[device.device_model_id]
            table = snapshot.point_tables[model.point_table_id]
            effective, _ = resolve_effective_device(device, model, table.protocol.name)
            devices.append(
                commander_pb2.Device(
                    device_id=str(device_id),
                    protocol=table.protocol.name,
                    endpoint=f"{effective.endpoint.host}:{effective.endpoint.port}",
                    revision=config.device_revision(str(device_id)) or "",
                    availability="DISABLED" if not device.enabled else "CONFIGURED",
                )
            )
        return devices

    async def ListDevices(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        try:
            devices = self._devices()
            if request.filter:
                devices = [device for device in devices if request.filter in device.device_id]
            start, end, token = _page(request.page_size, request.page_token, len(devices))
            return commander_pb2.ListDevicesResponse(
                devices=devices[start:end], next_page_token=token
            )
        except Exception as exc:
            await self._boundary.invalid(context, exc)

    async def GetDevice(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        try:
            return commander_pb2.GetDeviceResponse(
                device=next(item for item in self._devices() if item.device_id == request.id)
            )
        except StopIteration:
            await context.abort(grpc.StatusCode.NOT_FOUND, "device not found")
        except Exception as exc:
            await self._boundary.invalid(context, exc)

    async def ListPoints(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        try:
            config = self._boundary.service.config_manager
            snapshot = config.snapshot
            device = snapshot.site.devices.get(DeviceId(request.device_id))
            if device is None:
                raise KeyError(request.device_id)
            model = snapshot.device_models[device.device_model_id]
            table = snapshot.point_tables[model.point_table_id]
            points: list[common_pb2.PointMetadata] = []
            for point_id, point in sorted(table.points.items()):
                business = snapshot.business_points[point.business_point_id]
                address = []
                for key, value in sorted(point.ext.items()):
                    scalar = common_pb2.ScalarValue()
                    if type(value) is bool:
                        scalar.bool_value = value
                    elif type(value) is int:
                        if value < 0:
                            scalar.sint64_value = value
                        else:
                            scalar.uint64_value = value
                    elif isinstance(value, float):
                        scalar.double_value = value
                    elif isinstance(value, str):
                        scalar.string_value = value
                    else:
                        continue
                    address.append(common_pb2.AddressField(key=key, value=scalar))
                points.append(
                    common_pb2.PointMetadata(
                        point_id=point_id,
                        data_type=business.data_type.value,
                        unit=business.standard_unit.unit_id,
                        readable=point.access in (PointAccess.READ, PointAccess.READ_WRITE),
                        writable=point.access in (PointAccess.WRITE, PointAccess.READ_WRITE),
                        address=address,
                        revision=config.device_revision(request.device_id) or "",
                    )
                )
            start, end, token = _page(request.page_size, request.page_token, len(points))
            return commander_pb2.ListPointsResponse(points=points[start:end], next_page_token=token)
        except Exception as exc:
            await self._boundary.invalid(context, exc)

    @staticmethod
    def _command_spec(item: Any) -> CommandSpec:
        if not item.device_id or not item.point_id or not item.expected_revision:
            raise ValueError("device_id, point_id, expected_revision are required")
        if not item.HasField("value"):
            raise ValueError("value is required")
        expires = item.expires_at.ToDatetime(tzinfo=UTC) if item.HasField("expires_at") else None
        return CommandSpec(
            device_id=item.device_id,
            point_id=item.point_id,
            value=_scalar(item.value),
            config_reference=item.expected_revision,
            expires_at=expires,
        )

    def _command_result(self, result: CommandResult) -> Any:
        settled = self._boundary.service.executor.io_settled(
            result.command.command_id, result.state, result.error_code
        )
        return commander_pb2.GetCommandResultResponse(
            command_id=result.command.command_id,
            state=result.state.value,
            outcome=result.outcome.value if result.outcome is not None else "",
            error_code=result.error_code or "",
            message=result.error_message or "",
            device_id=result.command.spec.device_id,
            point_id=result.command.spec.point_id,
            updated_at=_timestamp(
                result.updated_at or result.completed_at or result.command.received_at
            ),
            io_settled=settled,
        )

    @staticmethod
    def _request_state(results: tuple[CommandResult, ...]) -> str:
        if any(result.state not in TERMINAL_STATES for result in results):
            if all(result.state is CommandState.ACCEPTED for result in results):
                return "ACCEPTED"
            return "EXECUTING"
        outcomes = {result.outcome for result in results}
        if CommandOutcome.UNKNOWN in outcomes:
            return "UNKNOWN"
        if outcomes == {CommandOutcome.CONFIRMED_SUCCESS}:
            return "SUCCEEDED"
        if outcomes == {CommandOutcome.NOT_SENT}:
            return "NOT_SENT"
        if outcomes <= {CommandOutcome.CONFIRMED_FAILURE, CommandOutcome.NOT_SENT}:
            return "FAILED"
        return "PARTIAL"

    async def SubmitCommand(self, request: Any, context: Any) -> Any:
        started = perf_counter()
        caller = await self._boundary.require(context, "control")
        try:
            if not request.request_id.strip():
                raise ValueError("request_id is required")
            spec = self._command_spec(request)
            admitted = await self._boundary.service.executor.accept(
                (spec,), caller_id=caller, request_id=request.request_id
            )
            results = await self._boundary.service.control.get_request(request.request_id, caller)
            return commander_pb2.SubmitCommandResponse(
                command_id=admitted[0].command_id,
                accepted=True,
                state=self._request_state(results),
                request_id=request.request_id,
            )
        except Exception as exc:
            await self._boundary.invalid(context, exc)
        finally:
            self._boundary.service.metrics.accept_latency((perf_counter() - started) * 1000)

    async def SubmitCommands(self, request: Any, context: Any) -> Any:
        started = perf_counter()
        caller = await self._boundary.require(context, "control")
        try:
            if not request.request_id.strip():
                raise ValueError("request_id is required")
            if not request.commands:
                raise ValueError("commands must not be empty")
            specs = tuple(self._command_spec(item) for item in request.commands)
            admitted = await self._boundary.service.executor.accept(
                specs, caller_id=caller, request_id=request.request_id
            )
            results = await self._boundary.service.control.get_request(request.request_id, caller)
            return commander_pb2.SubmitCommandsResponse(
                request_id=request.request_id,
                accepted=True,
                command_ids=[item.command_id for item in admitted],
                state=self._request_state(results),
            )
        except Exception as exc:
            await self._boundary.invalid(context, exc)
        finally:
            self._boundary.service.metrics.accept_latency((perf_counter() - started) * 1000)

    async def GetRequestResult(self, request: Any, context: Any) -> Any:
        started = perf_counter()
        caller = await self._boundary.require(context, "query")
        try:
            if not request.request_id.strip():
                raise ValueError("request_id is required")
            results = await self._boundary.service.control.get_request(request.request_id, caller)
            if not results:
                raise KeyError(request.request_id)
            children = [self._command_result(item) for item in results]
            updated = max(
                item.updated_at or item.completed_at or item.command.received_at for item in results
            )
            return commander_pb2.GetRequestResultResponse(
                request_id=request.request_id,
                state=self._request_state(results),
                commands=children,
                updated_at=_timestamp(updated),
                io_settled=all(child.io_settled for child in children),
            )
        except Exception as exc:
            await self._boundary.invalid(context, exc)
        finally:
            self._boundary.service.metrics.query_latency((perf_counter() - started) * 1000)

    async def GetCommandResult(self, request: Any, context: Any) -> Any:
        started = perf_counter()
        caller = await self._boundary.require(context, "query")
        try:
            if not request.command_id:
                raise ValueError("command_id is required")
            result = await self._boundary.service.control.get_command(request.command_id)
            if result is None or result.command.caller_id != caller:
                raise KeyError(request.command_id)
            return self._command_result(result)
        except Exception as exc:
            await self._boundary.invalid(context, exc)
        finally:
            self._boundary.service.metrics.query_latency((perf_counter() - started) * 1000)


class CommonRpc(common_pb2_grpc.CommonServiceServicer):
    def __init__(self, boundary: _Authorized, snapshots: Any) -> None:
        self._boundary = boundary
        self._snapshots = snapshots

    async def GetServiceInfo(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        response = common_pb2.GetServiceInfoResponse(
            service_id="commander",
            process_role="control",
            software_version=_software_version(),
            contract_version="v1",
        )
        started = self._boundary.service.started_at
        if started is not None:
            response.started_at.CopyFrom(_timestamp(started))
        return response

    async def GetServiceStatus(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        health = self._boundary.service.health()
        return common_pb2.GetServiceStatusResponse(
            health="HEALTHY" if health.ready else health.state.value,
            dependencies=[
                common_pb2.DependencyHealth(name=name, health="HEALTHY" if healthy else "DEGRADED")
                for name, healthy in (
                    ("persistence", health.persistence_operational),
                    ("journal", health.journal_operational),
                    ("audit", health.audit_operational),
                    ("dispatcher", health.dispatcher_operational),
                )
            ],
            observed_at=_timestamp(datetime.now(UTC)),
        )

    async def GetCapabilities(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        return common_pb2.GetCapabilitiesResponse(
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
                "CommanderService.ListDevices",
                "CommanderService.GetDevice",
                "CommanderService.ListPoints",
                "CommanderService.SubmitCommand",
                "CommanderService.SubmitCommands",
                "CommanderService.GetCommandResult",
                "CommanderService.GetRequestResult",
            ],
            protocols=self._boundary.service.supported_protocols,
            features=["single_point_control", "batch_control", "request_idempotency"],
        )

    async def GetConfigSnapshot(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        try:
            return await self._snapshots.get(request)
        except Exception as exc:
            await self._boundary.invalid(context, exc)

    async def PushConfigSnapshot(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "reload")
        try:
            return await self._snapshots.push(request)
        except Exception as exc:
            await self._boundary.invalid(context, exc)

    async def ApplyConfigSnapshot(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "reload")
        try:
            return await self._snapshots.apply(request)
        except Exception as exc:
            await self._boundary.invalid(context, exc)

    async def GetMetrics(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        service = self._boundary.service
        health = service.health()
        scope = request.scope
        if scope not in ("", "commander", "service") and not scope.startswith("device:"):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported metrics scope")
        window = int(request.window_ms)
        if window > 300_000:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "window_ms exceeds 300000")
        sampled = _timestamp(datetime.now(UTC))
        values: list[tuple[str, float, str, int]] = []
        if scope.startswith("device:"):
            device_id = scope.partition(":")[2]
            if not device_id:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "device ID is required")
            if DeviceId(device_id) not in service.config_manager.snapshot.site.devices:
                await context.abort(grpc.StatusCode.NOT_FOUND, "device not found")
            values.append(
                (
                    "device_queue_depth",
                    float(service.executor.device_queue_depth(device_id)),
                    "count",
                    0,
                )
            )
        else:
            try:
                counter_values = service.metrics.counters(window)
                latency_values = service.metrics.latencies(window)
            except (ValueError, RuntimeError) as exc:
                await self._boundary.invalid(context, exc)
                raise RuntimeError("unreachable") from exc
            for name, value in counter_values.items():
                values.append((name, float(value), "count", window))
            for name, latency_ms in latency_values.items():
                values.append((name, latency_ms, "ms", window or 300_000))
            values.extend(
                [
                    ("pending_global", float(health.pending_commands), "count", 0),
                    ("io_inflight", float(health.inflight_io), "count", 0),
                    ("blocked_devices", float(len(health.blocked_devices)), "count", 0),
                    ("service_ready", float(health.ready), "boolean", 0),
                ]
            )
            if health.journal_operational:
                values.extend(
                    [
                        ("repository_used_bytes", float(health.repository_used_bytes), "bytes", 0),
                        ("audit_pending_count", float(health.audit_pending_count), "count", 0),
                        ("audit_oldest_age_ms", float(health.audit_oldest_age_ms), "ms", 0),
                    ]
                )
            # Unavailable repository is unknown, not a proven failed write.
            # Do not fabricate repository_writable=0 without a write probe.
        return common_pb2.GetMetricsResponse(
            metrics=[
                common_pb2.Metric(
                    name=name,
                    value=value,
                    unit=unit,
                    window_ms=effective_window,
                    sampled_at=sampled,
                )
                for name, value, unit, effective_window in values
            ]
        )

    async def GetEvents(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        try:
            events = list(self._boundary.service.events())
            if (
                request.HasField("from_time")
                and request.HasField("to_time")
                and request.from_time.ToDatetime(tzinfo=UTC)
                > request.to_time.ToDatetime(tzinfo=UTC)
            ):
                raise ValueError("from_time must not exceed to_time")
            if request.HasField("from_time"):
                earliest = request.from_time.ToDatetime(tzinfo=UTC)
                events = [item for item in events if item.timestamp >= earliest]
            if request.HasField("to_time"):
                latest = request.to_time.ToDatetime(tzinfo=UTC)
                events = [item for item in events if item.timestamp <= latest]
            if request.filter:
                events = [
                    item
                    for item in events
                    if request.filter in item.message or request.filter in item.error_code
                ]
            start, end, token = _page(request.page_size, request.page_token, len(events))
            return common_pb2.GetEventsResponse(
                events=[
                    common_pb2.Event(
                        event_id=item.event_id,
                        timestamp=_timestamp(item.timestamp),
                        severity=item.severity,
                        source=item.source,
                        error_code=item.error_code,
                        message=item.message,
                    )
                    for item in events[start:end]
                ],
                next_page_token=token,
            )
        except Exception as exc:
            await self._boundary.invalid(context, exc)

    async def Diagnose(self, request: Any, context: Any) -> Any:
        await self._boundary.require(context, "query")
        stages = (
            tuple(request.stages)
            if request.stages
            else (
                "service",
                "configuration",
                "persistence",
                "audit",
            )
        )
        permitted = {
            "service",
            "configuration",
            "persistence",
            "audit",
            "device",
            "tcp",
            "protocol",
        }
        deadline = perf_counter() + (request.timeout_ms or 1000) / 1000
        if any(stage not in permitted for stage in stages):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported diagnostic stage")
        health = self._boundary.service.health()
        results = []
        for stage in stages:
            started = perf_counter()
            stage_status = ""
            stage_message = ""
            if started > deadline:
                results.append(
                    common_pb2.DiagnosticStage(
                        stage=stage,
                        status="TIMEOUT",
                        error_code="DEADLINE_EXCEEDED",
                        observed_at=_timestamp(datetime.now(UTC)),
                        source="commander",
                    )
                )
                continue
            if stage == "service":
                healthy = health.ready
                evidence = ("state", health.state.value)
            elif stage == "configuration":
                healthy = bool(health.revision)
                evidence = ("revision", health.revision)
            elif stage == "persistence":
                healthy = health.persistence_operational and health.journal_operational
                evidence = ("pending", str(health.audit_pending_count))
            elif stage == "audit":
                healthy = health.audit_operational
                evidence = ("published", str(health.audit_published))
            else:
                if not request.target_id:
                    await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "target_id required")
                snapshot = self._boundary.service.config_manager.snapshot
                device = snapshot.site.devices.get(DeviceId(request.target_id))
                healthy = device is not None and device.enabled
                evidence = ("configured", str(healthy))
                if stage == "protocol" and healthy and device is not None:
                    status, capabilities = self._boundary.service.executor.inspect_existing_session(
                        request.target_id
                    )
                    healthy = status == "CONNECTED"
                    if status in {"NOT_CONNECTED", "DISCONNECTED", "CLOSING"}:
                        stage_status = "NOT_RUN"
                        stage_message = (
                            "No settled existing protocol session available; "
                            "diagnostics never initiate control connections"
                        )
                    elif status == "BLOCKED":
                        stage_status = "FAIL"
                        stage_message = "Device has unsettled native I/O"
                    evidence = (
                        "session_capabilities",
                        ",".join(capabilities) if healthy else status,
                    )
                if stage == "tcp" and healthy and device is not None:
                    model = snapshot.device_models[device.device_model_id]
                    table = snapshot.point_tables[model.point_table_id]
                    effective, _ = resolve_effective_device(device, model, table.protocol.name)
                    host = effective.endpoint.host
                    port = effective.endpoint.port
                    if port is None:
                        healthy = False
                        evidence = ("error", "resolved endpoint has no TCP port")
                    else:
                        remaining = max(0.0, deadline - perf_counter())
                        try:
                            _, writer = await asyncio.wait_for(
                                asyncio.open_connection(host, port),
                                timeout=remaining,
                            )
                            writer.close()
                            await asyncio.wait_for(writer.wait_closed(), timeout=remaining)
                            evidence = ("tcp_endpoint", f"{host}:{port}")
                        except (OSError, TimeoutError):
                            healthy = False
                            evidence = ("tcp_endpoint", f"{host}:{port}")
            results.append(
                common_pb2.DiagnosticStage(
                    stage=stage,
                    status=stage_status or ("PASS" if healthy else "FAIL"),
                    message=stage_message,
                    duration_ms=int((perf_counter() - started) * 1000),
                    evidence=[common_pb2.Evidence(name=evidence[0], value=evidence[1])],
                    observed_at=_timestamp(datetime.now(UTC)),
                    source="commander",
                )
            )
        return common_pb2.DiagnoseResponse(
            overall_status=(
                "FAIL"
                if any(item.status in {"FAIL", "TIMEOUT"} for item in results)
                else "PARTIAL"
                if any(item.status == "NOT_RUN" for item in results)
                else "PASS"
            ),
            stages=results,
            limitations=[
                "TCP stage proves port reachability only, not protocol handshake",
                "Protocol stage reads existing session state only; no new handshake",
                "Dependency stages report aggregate health, not individual transport readiness",
            ],
        )


def register_services(
    server: grpc.aio.Server,
    service: CommanderService,
    permissions: dict[str, tuple[str, frozenset[str]]],
    snapshots: Any,
) -> None:
    """Register both frozen Proto services against a single Commander application."""
    boundary = _Authorized(service, permissions)
    commander_pb2_grpc.add_CommanderServiceServicer_to_server(CommanderRpc(boundary), server)
    common_pb2_grpc.add_CommonServiceServicer_to_server(CommonRpc(boundary, snapshots), server)
