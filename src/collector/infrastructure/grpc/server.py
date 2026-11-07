"""Collector gRPC adapter for the authoritative CollectorService contract."""

from __future__ import annotations

import asyncio
import hmac
import os
from datetime import UTC, datetime

import grpc

from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc as pb_grpc
from contracts import common_pb2
from contracts import common_pb2_grpc
from core.application.protocol_contract import InterrogationRequest, validate_read_many_results
from core.domain import DeviceId
from core.domain.point import engineering_value
from core.domain.value_objects import PointAccess
from core.infrastructure.rpc.scalar_codec import encode_scalar

from ...assembly import CollectorApp


def _page(items: list[str], size: int, token: str) -> tuple[list[str], str]:
    """Use a stable, exclusive item-ID cursor; never interpret user text as an offset."""
    if size > 1000:
        raise ValueError("page_size exceeds 1000")
    limit = size or 200
    start = 0
    if token:
        try:
            start = items.index(token) + 1
        except ValueError as exc:
            raise ValueError("invalid page_token") from exc
    selected = items[start : start + limit]
    next_token = selected[-1] if start + limit < len(items) else ""
    return selected, next_token


class CollectorGrpcServer:
    """Owns one CollectorService listener; never registers retired services."""

    def __init__(self, server: grpc.aio.Server, endpoint: str) -> None:
        self._server = server
        self.endpoint = endpoint

    async def start(self) -> None:
        await self._server.start()

    async def stop(self, grace: float = 5.0) -> None:
        await self._server.stop(grace)


class CollectorService(pb_grpc.CollectorServiceServicer):
    """Expose the current device registry and explicit Task operations."""

    def __init__(self, app: CollectorApp) -> None:
        self._app = app

    def _session(self, device_id: str):
        session = self._app.runtime.devices.get(device_id)
        if session is None:
            raise KeyError(f"unknown device: {device_id}")
        return session

    def _metadata(self, device_id: str, point_id: str) -> common_pb2.PointMetadata:
        session = self._session(device_id)
        point = session.point_table.points.get(point_id)
        if point is None:
            raise KeyError(f"unknown point: {device_id}/{point_id}")
        access = point.access
        message = common_pb2.PointMetadata(
            point_id=point_id,
            unit=point.source_unit.symbol,
            readable=access in {PointAccess.READ, PointAccess.READ_WRITE},
            writable=access in {PointAccess.WRITE, PointAccess.READ_WRITE},
            revision=self._app.config.active_revision,
        )
        for name, value in sorted(point.ext.items()):
            if value is not None:
                entry = message.address.add(key=name)
                entry.value.CopyFrom(encode_scalar(value))
        return message

    def _task(self, device_id: str) -> pb.Task:
        instances = self._app.runtime.task_instances()
        states = self._app.runtime.instance_states()
        desired = self._app.runtime.desired_states()
        item = instances.get(device_id)
        if item is None:
            raise KeyError(f"unknown task: {device_id}")
        state = states.get(device_id)
        return pb.Task(
            task_id=item.task_id,
            device_id=item.device_id,
            desired_state=desired.get(device_id, "STOP"),
            actual_state=state.value.upper() if state is not None else "UNKNOWN",
            revision=self._app.config.active_revision,
        )

    async def _abort(self, context: grpc.aio.ServicerContext, exc: Exception) -> None:
        if isinstance(exc, KeyError):
            status = grpc.StatusCode.NOT_FOUND
        elif isinstance(exc, ValueError):
            status = grpc.StatusCode.INVALID_ARGUMENT
        elif isinstance(exc, PermissionError):
            status = grpc.StatusCode.PERMISSION_DENIED
        elif isinstance(exc, TimeoutError):
            status = grpc.StatusCode.DEADLINE_EXCEEDED
        elif isinstance(exc, RuntimeError):
            status = grpc.StatusCode.FAILED_PRECONDITION
        else:
            status = grpc.StatusCode.INTERNAL
        await context.abort(status, str(exc))

    async def ListDevices(self, request: pb.ListDevicesRequest, context: grpc.aio.ServicerContext) -> pb.ListDevicesResponse:
        try:
            devices = sorted(d for d in self._app.runtime.devices if request.filter in d)
            selected, token = _page(devices, request.page_size, request.page_token)
            response = pb.ListDevicesResponse(next_page_token=token)
            for device_id in selected:
                session = self._session(device_id)
                response.devices.add(
                    device_id=device_id,
                    protocol=session.protocol_name,
                    endpoint=session.device.endpoint.host,
                    task_id=device_id,
                    actual_state="RUNNING" if session.is_open() else "STOPPED",
                    revision=self._app.config.active_revision,
                )
            return response
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def GetDevice(self, request: pb.DeviceQuery, context: grpc.aio.ServicerContext) -> pb.GetDeviceResponse:
        try:
            session = self._session(request.device_id)
            item = pb.Device(
                device_id=request.device_id,
                protocol=session.protocol_name,
                endpoint=session.device.endpoint.host,
                task_id=request.device_id,
                actual_state="RUNNING" if session.is_open() else "STOPPED",
                revision=self._app.config.active_revision,
            )
            return pb.GetDeviceResponse(device=item)
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def ListPoints(self, request: pb.ListPointsRequest, context: grpc.aio.ServicerContext) -> pb.ListPointsResponse:
        try:
            session = self._session(request.device_id)
            points = sorted(pid for pid in session.point_table.points if request.filter in pid)
            selected, token = _page(points, request.page_size, request.page_token)
            result = pb.ListPointsResponse(next_page_token=token)
            result.points.extend(self._metadata(request.device_id, pid) for pid in selected)
            return result
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def GetPoint(self, request: pb.PointQuery, context: grpc.aio.ServicerContext) -> pb.GetPointResponse:
        try:
            return pb.GetPointResponse(point=self._metadata(request.device_id, request.point_id))
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def ReadPoints(self, request: pb.ReadPointsRequest, context: grpc.aio.ServicerContext) -> pb.ReadPointsResponse:
        try:
            session = self._session(request.device_id)
            ids = list(request.point_ids)
            if not ids:
                raise ValueError("point_ids must not be empty")
            if len(ids) > 1024:
                raise ValueError("at most 1024 points per read")
            for point_id in ids:
                meta = self._metadata(request.device_id, point_id)
                if not meta.readable:
                    raise ValueError(f"point is not readable: {point_id}")
            if not session.is_open():
                raise RuntimeError("device is not connected")
            timeout = (request.timeout_ms / 1000) if request.timeout_ms else self._app.config.current_config.runtime.read_timeout
            async with asyncio.timeout(timeout):
                samples = await session.protocol.read_many(ids)
            validate_read_many_results(ids, samples)
            now = datetime.now(UTC)
            result = pb.ReadPointsResponse(device_id=request.device_id)
            result.queried_at.FromDatetime(now)
            for item in samples:
                point = session.point_table.points[item.point_id]
                converted = engineering_value(point, item.value)
                value = common_pb2.Sample(point_id=item.point_id, quality=item.quality.value, freshness="FRESH")
                if converted is not None:
                    value.value.CopyFrom(encode_scalar(converted))
                value.sample_timestamp.FromDatetime(item.timestamp)
                value.queried_at.FromDatetime(now)
                result.samples.append(value)
            return result
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def ListTasks(self, request: pb.ListTasksRequest, context: grpc.aio.ServicerContext) -> pb.ListTasksResponse:
        try:
            names = sorted(i for i in self._app.runtime.task_instances() if request.filter in i)
            selected, token = _page(names, request.page_size, request.page_token)
            return pb.ListTasksResponse(tasks=[self._task(i) for i in selected], next_page_token=token)
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def GetTask(self, request: common_pb2.ObjectId, context: grpc.aio.ServicerContext) -> pb.GetTaskResponse:
        try:
            return pb.GetTaskResponse(task=self._task(request.id))
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def _change_task(self, request: common_pb2.OperationRequest, context: grpc.aio.ServicerContext, *, start: bool) -> common_pb2.OperationResult:
        try:
            expected_token = os.getenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
            token = dict(context.invocation_metadata()).get("x-windhub-admin-token", "")
            if not expected_token or not hmac.compare_digest(token, expected_token):
                raise PermissionError("Collector task control requires admin credentials")
            if not request.expected_revision:
                raise ValueError("expected_revision is required")
            if request.expected_revision != self._app.config.active_revision:
                await context.abort(grpc.StatusCode.ABORTED, "task revision conflict")
            if start:
                await self._app.runtime.start_task_instance(request.id)
            else:
                await self._app.runtime.stop_task_instance(request.id)
            item = self._task(request.id)
            return common_pb2.OperationResult(
                operation_id=request.id,
                accepted=True,
                desired_state=item.desired_state,
                actual_state=item.actual_state,
                revision=item.revision,
            )
        except grpc.RpcError:
            raise
        except Exception as exc:
            await self._abort(context, exc)
            raise

    async def StartTask(self, request: common_pb2.OperationRequest, context: grpc.aio.ServicerContext) -> common_pb2.OperationResult:
        return await self._change_task(request, context, start=True)

    async def StopTask(self, request: common_pb2.OperationRequest, context: grpc.aio.ServicerContext) -> common_pb2.OperationResult:
        return await self._change_task(request, context, start=False)

    async def GetPublishingStatus(self, request: pb.GetPublishingStatusRequest, context: grpc.aio.ServicerContext) -> pb.GetPublishingStatusResponse:
        mode = str(self._app.boot_config.messaging.delivery)
        if request.channel and request.channel != mode:
            return pb.GetPublishingStatusResponse()
        status = pb.PublishingStatus(
            channel=mode,
            nats_connected=self._app.runtime.nats_connected,
            published=self._app.runtime.points_routed,
            failed=self._app.runtime.points_dropped,
        )
        return pb.GetPublishingStatusResponse(channels=[status])

    async def InterrogateDevice(self, request: pb.InterrogateDeviceRequest, context: grpc.aio.ServicerContext) -> pb.InterrogateDeviceResponse:
        try:
            session = self._session(request.device_id)
            if session.protocol_name != "iec104":
                await context.abort(grpc.StatusCode.UNIMPLEMENTED, "interrogation requires IEC104")
            address = request.common_address if request.HasField("common_address") else None
            timeout = (request.timeout_ms / 1000) if request.timeout_ms else self._app.config.current_config.runtime.read_timeout
            async with asyncio.timeout(timeout):
                samples = await session.protocol.interrogate(InterrogationRequest(common_address=address))
            now = datetime.now(UTC)
            result = pb.InterrogateDeviceResponse(status="SUCCESS")
            for item in samples:
                point = session.point_table.points.get(item.point_id)
                converted = engineering_value(point, item.value) if point is not None else item.value
                message = common_pb2.Sample(point_id=item.point_id, quality=item.quality.value, freshness="FRESH")
                if converted is not None:
                    message.value.CopyFrom(encode_scalar(converted))
                message.sample_timestamp.FromDatetime(item.timestamp)
                message.queried_at.FromDatetime(now)
                result.samples.append(message)
            return result
        except grpc.RpcError:
            raise
        except Exception as exc:
            await self._abort(context, exc)
            raise



class CollectorCommonService(common_pb2_grpc.CommonServiceServicer):
    """Shared operational queries over Collector's real in-process state."""

    def __init__(self, app: CollectorApp) -> None:
        self._app = app
        self._started = datetime.now(UTC)

    async def GetServiceInfo(self, request: common_pb2.Empty, context: grpc.aio.ServicerContext) -> common_pb2.GetServiceInfoResponse:
        response = common_pb2.GetServiceInfoResponse(
            service_id=self._app.identity.collector_id,
            process_role="collector",
            contract_version="v1",
        )
        response.started_at.FromDatetime(self._started)
        return response

    async def GetServiceStatus(self, request: common_pb2.Empty, context: grpc.aio.ServicerContext) -> common_pb2.GetServiceStatusResponse:
        runtime = self._app.runtime
        response = common_pb2.GetServiceStatusResponse(
            health="RUNNING" if runtime.running else "STOPPED",
        )
        response.observed_at.FromDatetime(datetime.now(UTC))
        response.dependencies.add(
            name="nats",
            health="RUNNING" if runtime.nats_connected else "UNAVAILABLE",
            detail="publisher connection",
        )
        return response

    async def GetCapabilities(self, request: common_pb2.Empty, context: grpc.aio.ServicerContext) -> common_pb2.GetCapabilitiesResponse:
        return common_pb2.GetCapabilitiesResponse(
            rpc_names=[method.name for method in pb.DESCRIPTOR.services_by_name["CollectorService"].methods],
            protocols=sorted({session.protocol_name for session in self._app.runtime.devices.values()}),
            features=["task_control", "batch_publication", "point_read", "observability"],
        )

    async def GetMetrics(self, request: common_pb2.GetMetricsRequest, context: grpc.aio.ServicerContext) -> common_pb2.GetMetricsResponse:
        now = datetime.now(UTC)
        if request.scope and request.scope not in {"service", "collector"}:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported metrics scope")
        if request.window_ms:
            if request.window_ms % 1000 or request.window_ms // 1000 not in self._app.metric_windows:
                await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported metrics window")
            values = self._app.metrics.window_snapshot(request.window_ms // 1000)
        else:
            values = self._app.metrics.snapshot()["counters"]
        result = common_pb2.GetMetricsResponse()
        for name, value in sorted(values.items()):
            metric = result.metrics.add(name=name, value=float(value), unit="count", window_ms=request.window_ms)
            metric.sampled_at.FromDatetime(now)
        return result

    async def GetEvents(self, request: common_pb2.GetEventsRequest, context: grpc.aio.ServicerContext) -> common_pb2.GetEventsResponse:
        if request.page_size > 500:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "page_size exceeds 500")
        events = self._app.metrics.snapshot()["events"]
        if request.filter:
            events = [event for event in events if request.filter in event["kind"] or request.filter in event["message"]]
        if request.HasField("from_time"):
            lower = request.from_time.ToDatetime(tzinfo=UTC)
            events = [event for event in events if datetime.fromisoformat(event["timestamp"]) >= lower]
        if request.HasField("to_time"):
            upper = request.to_time.ToDatetime(tzinfo=UTC)
            events = [event for event in events if datetime.fromisoformat(event["timestamp"]) <= upper]
        names = [str(index) for index in range(len(events))]
        try:
            indexes, token = _page(names, request.page_size or 200, request.page_token)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise
        result = common_pb2.GetEventsResponse(next_page_token=token)
        for index in indexes:
            event = events[int(index)]
            item = result.events.add(
                event_id=index,
                severity="WARNING" if "fail" in event["kind"] else "INFO",
                source=event["object"],
                error_code=event["kind"],
                message=event["message"],
            )
            item.timestamp.FromDatetime(datetime.fromisoformat(event["timestamp"]))
        return result

    async def Diagnose(self, request: common_pb2.DiagnoseRequest, context: grpc.aio.ServicerContext) -> common_pb2.DiagnoseResponse:
        if request.target_id and request.target_id not in self._app.runtime.devices:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown device")
        stage = common_pb2.DiagnosticStage(
            stage="publisher",
            status="PASS" if self._app.runtime.nats_connected else "UNKNOWN",
            source="collector.runtime",
        )
        stage.observed_at.FromDatetime(datetime.now(UTC))
        return common_pb2.DiagnoseResponse(
            overall_status=stage.status,
            stages=[stage],
            limitations=["Only the local publisher connection is inspected; no device I/O executed."],
        )

    async def GetConfigSnapshot(self, request: common_pb2.GetConfigSnapshotRequest, context: grpc.aio.ServicerContext) -> common_pb2.GetConfigSnapshotResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "configuration snapshot RPC requires a separately specified contract")
        raise AssertionError("unreachable")

    async def PushConfigSnapshot(self, request: common_pb2.PushConfigSnapshotRequest, context: grpc.aio.ServicerContext) -> common_pb2.PushConfigSnapshotResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "configuration snapshot RPC requires a separately specified contract")
        raise AssertionError("unreachable")

    async def ApplyConfigSnapshot(self, request: common_pb2.ApplyConfigSnapshotRequest, context: grpc.aio.ServicerContext) -> common_pb2.ApplyConfigSnapshotResponse:
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "configuration snapshot RPC requires a separately specified contract")
        raise AssertionError("unreachable")


def build_grpc_server(app: CollectorApp, *, host: str, port: int) -> CollectorGrpcServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("remote Collector gRPC requires a TLS-authenticated listener")
    server = grpc.aio.server()
    pb_grpc.add_CollectorServiceServicer_to_server(CollectorService(app), server)
    common_pb2_grpc.add_CommonServiceServicer_to_server(CollectorCommonService(app), server)
    bound = server.add_insecure_port(f"{host}:{port}")
    if not bound:
        raise RuntimeError("failed to bind Collector gRPC server")
    return CollectorGrpcServer(server, f"{host}:{bound}")
