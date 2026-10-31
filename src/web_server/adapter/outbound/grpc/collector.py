"""Collector v2 gRPC client and Server-side application DTO mapping."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC
from typing import cast
from uuid import uuid4

import grpc
from google.protobuf import empty_pb2

from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc
from web_server.adapter.outbound.grpc.common import GrpcClientBase
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


def _task_summary(message: pb.TaskStatus) -> CollectorTaskSummary:
    running = message.actual_state == pb.RUNNING
    return CollectorTaskSummary(
        task_id=message.task_id,
        device=message.device_id,
        device_group=None,
        point_group="",
        interval=message.interval_ms / 1000 if message.HasField("interval_ms") else None,
        targets=[],
        enabled=True,
        runtime_state="running" if running else "stopped",
        instance_count=1,
        running_instances=int(running),
        stopped_instances=int(not running),
        failed_instances=0,
    )


def _task_instance(message: pb.TaskStatus) -> CollectorTaskInstance:
    return CollectorTaskInstance(
        instance_id=message.device_id,
        task_id=message.task_id,
        device_id=message.device_id,
        point_group="",
        interval=message.interval_ms / 1000 if message.HasField("interval_ms") else None,
        targets=[],
        state="running" if message.actual_state == pb.RUNNING else "stopped",
    )


class CollectorGrpcClient(GrpcClientBase):
    """Collector v2 only; no placement or two-phase configuration RPCs."""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        super().__init__(target, default_timeout=default_timeout)
        self._runtime = pb_grpc.CollectorRuntimeServiceStub(self._channel)
        self._tasks = pb_grpc.CollectorTaskServiceStub(self._channel)
        self._config = pb_grpc.CollectorConfigServiceStub(self._channel)
        self._metrics = pb_grpc.CollectorMetricsServiceStub(self._channel)

    async def config_status(self) -> CollectorInfo:
        """Read Collector identity and runtime readiness without device I/O."""
        identity, runtime = await asyncio.gather(
            self._runtime.GetCollectorInfo(empty_pb2.Empty(), timeout=self.default_timeout),
            self._runtime.GetRuntimeStatus(empty_pb2.Empty(), timeout=self.default_timeout),
        )
        return CollectorInfo(
            collector_id=identity.collector_id,
            boot_id=identity.boot_id,
            active_revision=identity.active_revision,
            runtime_running=runtime.running,
        )

    async def runtime_status(self) -> CollectorRuntimeStatus:
        status, raw_metrics = await asyncio.gather(
            self._runtime.GetRuntimeStatus(empty_pb2.Empty(), timeout=self.default_timeout),
            self._metrics.GetMetrics(
                pb.GetMetricsRequest(page_size=200),
                timeout=self.default_timeout,
            ),
        )
        counters = {
            metric.name: int(metric.value)
            for metric in raw_metrics.metrics
            if not metric.HasField("device_id") and metric.valid
        }
        return CollectorRuntimeStatus(
            running=status.running,
            device_count=status.device_total,
            devices_connected=status.device_connected,
            points_collected=counters.get("points_total", 0),
            points_routed=0,
            points_dropped=0,
            acquisitions=[],
        )

    async def metrics_snapshot(self) -> MetricsSnapshot:
        raw, raw_events = await asyncio.gather(
            self._metrics.GetMetrics(
                pb.GetMetricsRequest(page_size=200),
                timeout=self.default_timeout,
            ),
            self._metrics.GetMetricsEvents(
                pb.GetMetricsEventsRequest(limit=1000),
                timeout=self.default_timeout,
            ),
        )
        total = {
            row.name: int(row.value)
            for row in raw.metrics
            if not row.HasField("device_id") and row.valid
        }
        device_failed: dict[str, int] = {}
        device_reconnected: dict[str, int] = {}
        for row in raw.metrics:
            if not row.valid or not row.HasField("device_id"):
                continue
            if row.name == "device_connect_failures":
                device_failed[row.device_id] = int(row.value)
            elif row.name == "device_reconnects":
                device_reconnected[row.device_id] = int(row.value)
        return MetricsSnapshot(
            counters=CounterSnapshot(
                points_total=total.get("points_total", 0),
                points_bad=total.get("points_bad", 0),
                acquisition_runs=total.get("acquisition_runs", 0),
                acquisition_failures=total.get("acquisition_failures", 0),
                acquisition_partial=total.get("acquisition_partial", 0),
                missed_cycles=total.get("missed_cycles", 0),
                poll_overruns=total.get("poll_overruns", 0),
                connect_failures=total.get("connect_failures", 0),
                reconnects=total.get("reconnects", 0),
            ),
            device_connect_failures=device_failed,
            device_reconnects=device_reconnected,
            events=[
                MonitoringEvent(
                    timestamp=event.timestamp.ToDatetime(tzinfo=UTC),
                    kind=event.event_type,
                    object=event.device_id if event.HasField("device_id") else "",
                    message=event.message,
                )
                for event in raw_events.events
            ],
        )

    async def list_devices(self) -> list[DeviceRuntimeInfo]:
        """Read all device pages; never silently return only the first 200."""
        rows: list[DeviceRuntimeInfo] = []
        token = ""
        while True:
            response = await self._runtime.ListDevices(
                pb.ListDevicesRequest(page_size=200, page_token=token),
                timeout=self.default_timeout,
            )
            rows.extend(
                DeviceRuntimeInfo(
                    device_id=item.device_id,
                    protocol=item.protocol,
                    connected=item.connected,
                    last_seen=(
                        item.last_connected_at.ToJsonString()
                        if item.HasField("last_connected_at")
                        else None
                    ),
                    consecutive_failures=item.consecutive_failures,
                    last_error=(item.last_error if item.HasField("last_error") else None),
                )
                for item in response.devices
            )
            token = response.next_page_token
            if not token:
                return rows

    async def _list_task_statuses(self) -> list[pb.TaskStatus]:
        rows: list[pb.TaskStatus] = []
        token = ""
        while True:
            response = await self._tasks.ListTasks(
                pb.ListTasksRequest(page_size=200, page_token=token),
                timeout=self.default_timeout,
            )
            rows.extend(response.tasks)
            token = response.next_page_token
            if not token:
                return rows

    async def list_tasks(self) -> list[CollectorTaskSummary]:
        return [_task_summary(item) for item in await self._list_task_statuses()]

    async def list_task_instances(self) -> list[CollectorTaskInstance]:
        return [_task_instance(item) for item in await self._list_task_statuses()]

    @staticmethod
    def _admin_metadata() -> tuple[tuple[str, str], ...]:
        token = os.getenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
        if not token:
            raise PermissionError("Collector task control requires admin credentials")
        return (("x-windhub-admin-token", token),)

    async def _set_task(self, device_id: str, *, start: bool) -> pb.TaskStatus:
        """Use Collector's revision transaction, not direct task handle mutation."""
        metadata = self._admin_metadata()
        status = await self._config.GetConfigStatus(
            pb.GetConfigStatusRequest(
                selectors=[pb.ConfigSelector(scope=pb.INSTANCE, name="devices")]
            ),
            timeout=self.default_timeout,
            metadata=metadata,
        )
        if not status.configs:
            raise RuntimeError("Collector devices config revision unavailable")
        revision = status.configs[0].stored_revision
        request = pb.TaskChangeRequest(
            device_id=device_id,
            expected_revision=revision,
            request_id=uuid4().hex,
        )
        operation = self._tasks.StartTask if start else self._tasks.StopTask
        try:
            await operation(request, timeout=self.default_timeout, metadata=metadata)
        except grpc.aio.AioRpcError as exc:
            if exc.code() in (
                grpc.StatusCode.ABORTED,
                grpc.StatusCode.FAILED_PRECONDITION,
            ):
                raise CollectorTaskConflictError(exc.details() or exc.code().name) from exc
            raise
        # grpcio ships no type stubs, so generated stub calls resolve to Any.
        return cast(
            pb.TaskStatus,
            await self._tasks.GetTask(
                pb.DeviceIdRequest(device_id=device_id),
                timeout=self.default_timeout,
            ),
        )

    async def start_task(self, device_id: str) -> CollectorTaskSummary:
        return _task_summary(await self._set_task(device_id, start=True))

    async def stop_task(self, device_id: str) -> CollectorTaskSummary:
        return _task_summary(await self._set_task(device_id, start=False))

    async def start_task_instance(self, device_id: str) -> CollectorTaskInstance:
        return _task_instance(await self._set_task(device_id, start=True))

    async def stop_task_instance(self, device_id: str) -> CollectorTaskInstance:
        return _task_instance(await self._set_task(device_id, start=False))
