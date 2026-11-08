"""Collector v1 gRPC 出站适配器；仅使用当前正式 Collector/Common 契约。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC

import grpc

from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc as pb_grpc
from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_grpc
from core.domain.sample_value import Quality
from web_server.adapter.outbound.grpc.common import GrpcClientBase
from web_server.application.device.sample import PointValue
from web_server.application.port.monitoring import (
    CounterSnapshot,
    MetricsSnapshot,
    MonitoringEvent,
)
from web_server.application.port.worker import (
    CollectorInfo,
    CollectorRuntimeStatus,
    CollectorTaskConflictError,
    CollectorTaskInstance,
    CollectorTaskSummary,
    DeviceRuntimeInfo,
)

_RUNNING = {"RUNNING"}
_CONNECTED = {"RUNNING"}
_FAILED = {"FAULTED", "ISOLATED", "BLOCKED"}


def _task_summary(message: pb.Task) -> CollectorTaskSummary:
    running = message.actual_state in _RUNNING
    failed = message.actual_state in _FAILED
    return CollectorTaskSummary(
        task_id=message.task_id,
        device=message.device_id,
        device_group=None,
        point_group="",
        interval=None,
        targets=[],
        enabled=message.desired_state == "START",
        runtime_state="failed" if failed else "running" if running else "stopped",
        instance_count=1,
        running_instances=int(running),
        stopped_instances=int(not running and not failed),
        failed_instances=int(failed),
    )


def _task_instance(message: pb.Task) -> CollectorTaskInstance:
    return CollectorTaskInstance(
        instance_id=message.device_id,
        task_id=message.task_id,
        device_id=message.device_id,
        point_group="",
        interval=None,
        targets=[],
        state=(
            "failed"
            if message.actual_state in _FAILED
            else "running"
            if message.actual_state in _RUNNING
            else "stopped"
        ),
    )


class CollectorGrpcClient(GrpcClientBase):
    """Collector v1 只读快照和带磁盘修订号的任务控制。"""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        super().__init__(target, default_timeout=default_timeout)
        self._collector = pb_grpc.CollectorServiceStub(self._channel)
        self._common = common_grpc.CommonServiceStub(self._channel)

    @staticmethod
    def _admin_metadata() -> tuple[tuple[str, str], ...]:
        token = os.getenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
        if not token:
            raise PermissionError("Collector management RPC requires admin credentials")
        return (("x-windhub-admin-token", token),)

    async def config_status(self) -> CollectorInfo:
        """服务实例标识由 Collector 的实际运行时身份提供。"""
        info, status, config = await asyncio.gather(
            self._common.GetServiceInfo(common.Empty(), timeout=self.default_timeout),
            self._common.GetServiceStatus(common.Empty(), timeout=self.default_timeout),
            self._common.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME),
                timeout=self.default_timeout,
                metadata=self._admin_metadata(),
            ),
        )
        if info.process_role != "collector" or not info.service_id:
            raise RuntimeError(f"unexpected service role: {info.process_role}")
        return CollectorInfo(
            collector_id=info.service_id,
            boot_id="",
            active_revision=config.revision,
            runtime_running=status.health in {"HEALTHY", "DEGRADED"},
        )

    async def runtime_status(self) -> CollectorRuntimeStatus:
        status, devices, metrics = await asyncio.gather(
            self._common.GetServiceStatus(common.Empty(), timeout=self.default_timeout),
            self.list_devices(),
            self._common.GetMetrics(
                common.GetMetricsRequest(scope="service"),
                timeout=self.default_timeout,
            ),
        )
        values = {metric.name: metric.value for metric in metrics.metrics}
        return CollectorRuntimeStatus(
            running=status.health in {"HEALTHY", "DEGRADED"},
            device_count=len(devices),
            devices_connected=sum(device.connected for device in devices),
            points_collected=int(values.get("collector.points.collected", 0)),
            points_routed=int(values.get("collector.publisher.published", 0)),
            points_dropped=int(values.get("collector.publisher.dropped", 0)),
            acquisitions=[],
        )

    async def metrics_snapshot(self) -> MetricsSnapshot:
        response = await self._common.GetMetrics(
            common.GetMetricsRequest(scope="service"), timeout=self.default_timeout
        )
        values = {metric.name: metric.value for metric in response.metrics}
        counters = CounterSnapshot(
            points_total=int(values.get("collector.points.collected", 0)),
            points_bad=int(values.get("collector.points.bad", 0)),
            acquisition_runs=int(values.get("collector.poll.executions", 0)),
            acquisition_failures=int(values.get("collector.poll.failed", 0)),
            acquisition_partial=int(values.get("collector.poll.partial_success", 0)),
            missed_cycles=int(values.get("collector.poll.skipped_slots", 0)),
        )
        events: list[MonitoringEvent] = []
        token = os.getenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
        if token:
            page_token = ""
            while len(events) < 1000:
                result = await self._common.GetEvents(
                    common.GetEventsRequest(page_size=200, page_token=page_token),
                    timeout=self.default_timeout,
                    metadata=(("x-windhub-admin-token", token),),
                )
                events.extend(
                    MonitoringEvent(
                        timestamp=item.timestamp.ToDatetime(tzinfo=UTC),
                        kind=item.severity,
                        object=item.source,
                        message=item.message,
                    )
                    for item in result.events
                )
                page_token = result.next_page_token
                if not page_token:
                    break
        return MetricsSnapshot(counters=counters, events=events[:1000])

    async def list_devices(self) -> list[DeviceRuntimeInfo]:
        rows: list[DeviceRuntimeInfo] = []
        token = ""
        while True:
            response = await self._collector.ListDevices(
                pb.ListDevicesRequest(page_size=200, page_token=token),
                timeout=self.default_timeout,
            )
            rows.extend(
                DeviceRuntimeInfo(
                    device_id=item.device_id,
                    protocol=item.protocol,
                    connected=item.actual_state in _CONNECTED,
                    last_seen=None,
                    consecutive_failures=0,
                    last_error=None,
                )
                for item in response.devices
            )
            token = response.next_page_token
            if not token:
                return rows

    async def read_points(
        self,
        device_id: str,
        point_ids: list[str],
        *,
        timeout: float | None = None,
    ) -> list[PointValue]:
        """调用 Collector 的实时协议读点，不读取缓存，不伪造失败样本。"""
        if not point_ids:
            return []
        if len(point_ids) > 500 or len(set(point_ids)) != len(point_ids):
            raise ValueError("Collector ReadPoints requires 1..500 unique point IDs")
        deadline = self.default_timeout if timeout is None else timeout
        if deadline <= 0:
            raise ValueError("read timeout must be positive")
        response = await self._collector.ReadPoints(
            pb.ReadPointsRequest(
                device_id=device_id,
                point_ids=point_ids,
                timeout_ms=max(1, int(deadline * 1000)),
            ),
            timeout=deadline,
            metadata=self._admin_metadata(),
        )
        observed: dict[str, PointValue] = {}
        for sample in response.samples:
            if sample.point_id not in point_ids or sample.point_id in observed:
                raise ValueError("Collector returned unexpected or duplicate point ID")
            try:
                quality = Quality(sample.quality)
            except ValueError as exc:
                raise ValueError(f"invalid sample quality: {sample.quality}") from exc
            if sample.HasField("value"):
                kind = sample.value.WhichOneof("kind")
                if kind is None or kind == "bytes_value":
                    raise ValueError("unsupported Collector sample scalar")
                value = getattr(sample.value, kind)
            else:
                value = None
            if sample.HasField("sample_timestamp"):
                timestamp = sample.sample_timestamp.ToDatetime(tzinfo=UTC)
            elif sample.HasField("queried_at"):
                timestamp = sample.queried_at.ToDatetime(tzinfo=UTC)
            else:
                raise ValueError("Collector sample has no timestamp")
            observed[sample.point_id] = PointValue(
                device_id=device_id,
                point_id=sample.point_id,
                value=value,
                quality=quality,
                timestamp=timestamp,
                source="collector",
                timestamp_source="device" if sample.HasField("sample_timestamp") else "local",
            )
        if set(observed) != set(point_ids):
            raise ValueError("Collector omitted one or more requested point results")
        return [observed[point_id] for point_id in point_ids]

    async def _list_tasks(self) -> list[pb.Task]:
        rows: list[pb.Task] = []
        token = ""
        while True:
            response = await self._collector.ListTasks(
                pb.ListTasksRequest(page_size=200, page_token=token),
                timeout=self.default_timeout,
            )
            rows.extend(response.tasks)
            token = response.next_page_token
            if not token:
                return rows

    async def list_tasks(self) -> list[CollectorTaskSummary]:
        return [_task_summary(task) for task in await self._list_tasks()]

    async def list_task_instances(self) -> list[CollectorTaskInstance]:
        return [_task_instance(task) for task in await self._list_tasks()]

    async def _set_task(self, device_id: str, *, start: bool) -> pb.Task:
        previous = await self._collector.GetTask(
            common.ObjectId(id=device_id), timeout=self.default_timeout
        )
        operation = self._collector.StartTask if start else self._collector.StopTask
        try:
            result = await operation(
                common.OperationRequest(id=device_id, expected_revision=previous.task.revision),
                timeout=self.default_timeout,
                metadata=self._admin_metadata(),
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() in (grpc.StatusCode.ABORTED, grpc.StatusCode.FAILED_PRECONDITION):
                raise CollectorTaskConflictError(exc.details() or exc.code().name) from exc
            raise
        if not result.accepted:
            raise CollectorTaskConflictError(result.message or result.error_code)
        refreshed = await self._collector.GetTask(
            common.ObjectId(id=device_id), timeout=self.default_timeout
        )
        return refreshed.task

    async def start_task(self, device_id: str) -> CollectorTaskSummary:
        return _task_summary(await self._set_task(device_id, start=True))

    async def stop_task(self, device_id: str) -> CollectorTaskSummary:
        return _task_summary(await self._set_task(device_id, start=False))

    async def start_task_instance(self, device_id: str) -> CollectorTaskInstance:
        return _task_instance(await self._set_task(device_id, start=True))

    async def stop_task_instance(self, device_id: str) -> CollectorTaskInstance:
        return _task_instance(await self._set_task(device_id, start=False))
