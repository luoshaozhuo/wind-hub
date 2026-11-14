"""CollectorService gRPC adapter for live devices, tasks and protocol operations."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar

import grpc

from collector.application.acquisition import CollectorAcquisition, DeviceWorker
from collector.application.config_models import SnapshotConflictError, SnapshotError
from collector.application.configuration import CollectorConfigurationManager
from collector.infrastructure.adapters.inbound.grpc.config_service import CollectorConfigRpc
from collector.infrastructure.config.task_control import CollectorTaskControl
from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc as rpc
from contracts import common_pb2 as common
from contracts.scalar_codec import encode_scalar
from core.application import InterrogationRequest, ProtocolSample
from core.application.errors import ConfigError, CoreError
from core.domain import PointAccess

T = TypeVar("T")


def _page(items: list[T], size: int, token: str) -> tuple[list[T], str]:
    if token and (not token.isascii() or not token.isdecimal()):
        raise ValueError("invalid pagination token")
    start = int(token) if token else 0
    if start > len(items):
        raise ValueError("pagination token outside collection")
    limit = min(size or 100, 500)
    end = min(start + limit, len(items))
    return items[start:end], (str(end) if end < len(items) else "")


def _scalar(value: object) -> common.ScalarValue | None:
    if value is None:
        return None
    return encode_scalar(value)


def _sample(item: ProtocolSample) -> common.Sample:
    response = common.Sample(
        point_id=item.point_id,
        quality=item.quality.value,
        freshness="LIVE",
    )
    try:
        scalar = _scalar(item.value)
    except ValueError:
        scalar = None
    if scalar is not None:
        response.value.CopyFrom(scalar)
    elif item.value is not None:
        response.quality = "bad"
        response.error_code = "UNSUPPORTED_VALUE"
        response.error_message = "sample cannot be encoded as ScalarValue"
    response.sample_timestamp.FromDatetime(item.timestamp)
    response.queried_at.FromDatetime(datetime.now(UTC))
    return response


class CollectorDeviceRpc(rpc.CollectorServiceServicer):
    """Translate Collector's application models into the fixed 11 RPC contract."""

    def __init__(
        self,
        acquisition: CollectorAcquisition,
        configuration: CollectorConfigurationManager,
        control: CollectorTaskControl,
        publisher: object,
    ) -> None:
        self._acquisition = acquisition
        self._configuration = configuration
        self._control = control
        self._publisher = publisher

    def _worker(self, device_id: str) -> DeviceWorker:
        worker = self._acquisition.workers.get(device_id)
        if worker is None:
            raise LookupError(f"device does not exist: {device_id}")
        return worker

    @staticmethod
    async def _error(context: grpc.aio.ServicerContext[Any, Any], exc: Exception) -> None:
        if isinstance(exc, LookupError):
            code = grpc.StatusCode.NOT_FOUND
        elif isinstance(exc, SnapshotConflictError):
            code = grpc.StatusCode.ABORTED
        elif isinstance(exc, SnapshotError | CoreError):
            # SnapshotError subclasses ValueError: it must be classified
            # before the generic argument branch below, or every snapshot
            # precondition degrades into INVALID_ARGUMENT.
            code = grpc.StatusCode.FAILED_PRECONDITION
        elif isinstance(exc, ConfigError | ValueError | TypeError):
            code = grpc.StatusCode.INVALID_ARGUMENT
        elif isinstance(exc, TimeoutError):
            code = grpc.StatusCode.DEADLINE_EXCEEDED
        else:
            code = grpc.StatusCode.FAILED_PRECONDITION
        await context.abort(code, str(exc))

    @staticmethod
    def _device(item: DeviceWorker, revision: str) -> pb.Device:
        device = item.plan.device
        return pb.Device(
            device_id=str(device.device_id),
            protocol=item.plan.point_table.protocol.name,
            endpoint=f"{device.endpoint.host}:{device.endpoint.port or 0}",
            task_id=str(device.device_id),
            actual_state=item.actual_state,
            revision=item.applied_revision,
        )

    @staticmethod
    def _task(item: DeviceWorker, revision: str) -> pb.Task:
        device_id = str(item.plan.device.device_id)
        return pb.Task(
            task_id=device_id,
            device_id=device_id,
            desired_state=item.desired_state,
            actual_state=item.actual_state,
            revision=revision,
            last_error=item.last_error,
        )

    @staticmethod
    def _point(item: DeviceWorker, point_id: str, revision: str) -> common.PointMetadata:
        point = item.plan.point_table.points[point_id]
        result = common.PointMetadata(
            point_id=point_id,
            data_type=item.plan.data_types.get(point_id, ""),
            unit=str(point.source_unit.symbol),
            readable=point.access in (PointAccess.READ, PointAccess.READ_WRITE),
            writable=point.access in (PointAccess.WRITE, PointAccess.READ_WRITE),
            revision=item.applied_revision,
        )
        for key, value in sorted(point.ext.items()):
            field = result.address.add(key=key)
            scalar = _scalar(value)
            if scalar is not None:
                field.value.CopyFrom(scalar)
        return result

    async def ListDevices(  # noqa: N802
        self, request: pb.ListDevicesRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.ListDevicesResponse:
        try:
            revision = self._configuration.status().runtime_revision
            devices = [
                self._device(worker, revision)
                for key, worker in sorted(self._acquisition.workers.items())
                if not request.filter or request.filter in key
            ]
            selected, token = _page(devices, request.page_size, request.page_token)
            return pb.ListDevicesResponse(devices=selected, next_page_token=token)
        except ValueError as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def GetDevice(  # noqa: N802
        self, request: pb.DeviceQuery, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetDeviceResponse:
        try:
            return pb.GetDeviceResponse(
                device=self._device(
                    self._worker(request.device_id),
                    self._configuration.status().disk_revision,
                )
            )
        except LookupError as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def ListPoints(  # noqa: N802
        self, request: pb.ListPointsRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.ListPointsResponse:
        try:
            worker = self._worker(request.device_id)
            revision = self._configuration.status().runtime_revision
            points = [
                self._point(worker, key, revision)
                for key in sorted(worker.plan.point_table.points)
                if not request.filter or request.filter in key
            ]
            selected, token = _page(points, request.page_size, request.page_token)
            return pb.ListPointsResponse(points=selected, next_page_token=token)
        except (LookupError, ValueError) as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def GetPoint(  # noqa: N802
        self, request: pb.PointQuery, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetPointResponse:
        try:
            worker = self._worker(request.device_id)
            if request.point_id not in worker.plan.point_table.points:
                raise LookupError("unknown point")
            return pb.GetPointResponse(
                point=self._point(
                    worker,
                    request.point_id,
                    self._configuration.status().runtime_revision,
                )
            )
        except LookupError as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def ReadPoints(  # noqa: N802
        self, request: pb.ReadPointsRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.ReadPointsResponse:
        await CollectorConfigRpc._authorize(context)
        try:
            worker = self._worker(request.device_id)
            ids = tuple(request.point_ids) or tuple(
                point.point_id
                for point in worker.plan.point_table.points.values()
                if point.access in (PointAccess.READ, PointAccess.READ_WRITE)
            )
            if len(ids) > 500 or len(set(ids)) != len(ids):
                raise ValueError("ReadPoints requires 1..500 unique readable points")
            if request.timeout_ms:
                timeout = min(request.timeout_ms / 1000, worker.timeout)
                samples = await asyncio.wait_for(asyncio.shield(worker.submit_read(ids)), timeout)
            else:
                samples = await asyncio.wait_for(
                    asyncio.shield(worker.submit_read(ids)), worker.timeout
                )
            received = {sample.point_id: sample for sample in samples}
            response = pb.ReadPointsResponse(device_id=request.device_id)
            for point_id in ids:
                if point_id in received:
                    response.samples.append(_sample(received[point_id]))
                else:
                    missing = response.samples.add(
                        point_id=point_id,
                        quality="bad",
                        freshness="LIVE",
                        error_code="POINT_NOT_RETURNED",
                        error_message="protocol did not return the requested point",
                    )
                    missing.queried_at.FromDatetime(datetime.now(UTC))
            response.queried_at.FromDatetime(datetime.now(UTC))
            return response
        except TimeoutError:
            response = pb.ReadPointsResponse(device_id=request.device_id)
            response.queried_at.FromDatetime(datetime.now(UTC))
            for point_id in ids:
                missing = response.samples.add(
                    point_id=point_id,
                    quality="bad",
                    freshness="LIVE",
                    error_code="TIMEOUT_UNCONFIRMED",
                    error_message="active protocol read has not safely settled",
                )
                missing.queried_at.CopyFrom(response.queried_at)
            return response
        except (LookupError, ValueError, RuntimeError, CoreError) as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def ListTasks(  # noqa: N802
        self, request: pb.ListTasksRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.ListTasksResponse:
        try:
            revision = self._configuration.status().disk_revision
            tasks = [
                self._task(worker, revision)
                for key, worker in sorted(self._acquisition.workers.items())
                if not request.filter or request.filter in key
            ]
            selected, token = _page(tasks, request.page_size, request.page_token)
            return pb.ListTasksResponse(tasks=selected, next_page_token=token)
        except ValueError as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def GetTask(  # noqa: N802
        self, request: common.ObjectId, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetTaskResponse:
        try:
            # Task.revision is the writable (DISK) revision clients must echo in
            # StartTask/StopTask expected_revision; runtime may lag during
            # partial convergence and must not poison optimistic concurrency.
            revision = self._configuration.status().disk_revision
            return pb.GetTaskResponse(task=self._task(self._worker(request.id), revision))
        except LookupError as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def _control_task(
        self,
        request: common.OperationRequest,
        context: grpc.aio.ServicerContext[Any, Any],
        state: str,
    ) -> common.OperationResult:
        await CollectorConfigRpc._authorize(context)
        try:
            status = await self._control.set_desired(
                request.id, state, expected_revision=request.expected_revision
            )
            current = self._acquisition.workers.get(request.id)
            return common.OperationResult(
                operation_id=uuid.uuid4().hex,
                accepted=True,
                desired_state=state,
                actual_state=current.actual_state if current is not None else "STOPPED",
                revision=status.disk_revision,
                error_code="CONVERGENCE_PENDING" if status.apply_errors else "",
                message="; ".join(status.apply_errors.values()),
            )
        except (LookupError, ValueError, RuntimeError, CoreError, SnapshotError) as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc

    async def StartTask(  # noqa: N802
        self, request: common.OperationRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common.OperationResult:
        return await self._control_task(request, context, "START")

    async def StopTask(  # noqa: N802
        self, request: common.OperationRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> common.OperationResult:
        return await self._control_task(request, context, "STOP")

    async def GetPublishingStatus(  # noqa: N802
        self, request: pb.GetPublishingStatusRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.GetPublishingStatusResponse:
        del context
        mode = getattr(self._publisher, "_delivery", "realtime")
        if request.channel and request.channel != mode:
            return pb.GetPublishingStatusResponse()
        connection = getattr(self._publisher, "_connection", None)
        return pb.GetPublishingStatusResponse(
            channels=[
                pb.PublishingStatus(
                    channel=mode,
                    nats_connected=bool(connection and connection.is_connected),
                    published=getattr(self._publisher, "published", 0),
                    failed=getattr(self._publisher, "failed", 0),
                    backpressure=getattr(self._publisher, "backpressure", 0),
                    last_error=getattr(self._publisher, "last_error", ""),
                )
            ]
        )

    async def InterrogateDevice(  # noqa: N802
        self, request: pb.InterrogateDeviceRequest, context: grpc.aio.ServicerContext[Any, Any]
    ) -> pb.InterrogateDeviceResponse:
        await CollectorConfigRpc._authorize(context)
        try:
            worker = self._worker(request.device_id)
            if worker.plan.point_table.protocol.name != "iec104":
                raise ValueError("interrogation requires IEC104")
            interrogation = InterrogationRequest(
                common_address=(
                    request.common_address if request.HasField("common_address") else None
                )
            )
            # An active interrogation owns the device session; never await it
            # without a deadline (timeout_ms=0 uses the server upper bound).
            if request.timeout_ms:
                timeout = min(request.timeout_ms / 1000, worker.timeout)
            else:
                timeout = worker.timeout
            samples = await asyncio.wait_for(
                asyncio.shield(worker.submit_interrogation(interrogation)),
                timeout,
            )
            observed = {sample.point_id for sample in samples}
            readable = {
                point.point_id
                for point in worker.plan.point_table.points.values()
                if point.access in (PointAccess.READ, PointAccess.READ_WRITE)
            }
            missing = sorted(readable - observed)
            return pb.InterrogateDeviceResponse(
                status="PARTIAL" if missing else "SUCCESS",
                samples=[_sample(sample) for sample in samples],
                missing_point_ids=missing,
            )
        except TimeoutError as exc:
            return pb.InterrogateDeviceResponse(
                status="UNKNOWN",
                error_code="TIMEOUT_UNCONFIRMED",
                error_message=str(exc) or "interrogation result is unconfirmed",
            )
        except (LookupError, ValueError, RuntimeError, CoreError) as exc:
            await self._error(context, exc)
            raise AssertionError("aborted") from exc
