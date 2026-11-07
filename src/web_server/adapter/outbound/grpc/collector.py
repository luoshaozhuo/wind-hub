"""Outbound Collector client for the authoritative CollectorService contract."""

from __future__ import annotations

import os
from datetime import UTC

import grpc

from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc as pb_grpc
from contracts import common_pb2
from contracts import common_pb2_grpc
from web_server.adapter.outbound.grpc.common import GrpcClientBase
from web_server.application.port.monitoring import CounterSnapshot, MetricsSnapshot, MonitoringEvent
from web_server.application.port.worker import (
    CollectorInfo,
    CollectorRuntimeStatus,
    CollectorTaskConflictError,
    CollectorTaskInstance,
    CollectorTaskSummary,
    DeviceRuntimeInfo,
)


def _summary(message: pb.Task) -> CollectorTaskSummary:
    running = message.actual_state.upper() == "RUNNING"
    return CollectorTaskSummary(
        task_id=message.task_id,
        device=message.device_id,
        device_group=None,
        point_group="",
        interval=None,
        targets=[],
        enabled=True,
        runtime_state=message.actual_state.lower(),
        instance_count=1,
        running_instances=int(running),
        stopped_instances=int(not running),
        failed_instances=0,
    )


def _instance(message: pb.Task) -> CollectorTaskInstance:
    return CollectorTaskInstance(
        instance_id=message.device_id,
        task_id=message.task_id,
        device_id=message.device_id,
        point_group="",
        interval=None,
        targets=[],
        state=message.actual_state.lower(),
    )


class CollectorGrpcClient(GrpcClientBase):
    """Use exactly the 11 published Collector RPCs; reject removed queries."""

    def __init__(self, target: str, *, default_timeout: float = 5.0) -> None:
        super().__init__(target, default_timeout=default_timeout)
        self._collector = pb_grpc.CollectorServiceStub(self._channel)
        self._common = common_pb2_grpc.CommonServiceStub(self._channel)

    async def config_status(self) -> CollectorInfo:
        """Read authoritative identity; absent boot ID is left unspecified."""
        info = await self._common.GetServiceInfo(
            common_pb2.Empty(), timeout=self.default_timeout
        )
        health = await self._common.GetServiceStatus(
            common_pb2.Empty(), timeout=self.default_timeout
        )
        tasks = await self._tasks()
        revision = tasks[0].revision if tasks else ""
        if not revision:
            devices = await self._collector.ListDevices(
                pb.ListDevicesRequest(page_size=1),
                timeout=self.default_timeout,
            )
            revision = devices.devices[0].revision if devices.devices else ""
        return CollectorInfo(
            collector_id=info.service_id,
            boot_id="",
            active_revision=revision,
            runtime_running=health.health == "RUNNING",
        )

    async def runtime_status(self) -> CollectorRuntimeStatus:
        """Aggregate only the counters the official RPCs actually provide."""
        health = await self._common.GetServiceStatus(
            common_pb2.Empty(), timeout=self.default_timeout
        )
        devices = await self.list_devices()
        metrics = await self._common.GetMetrics(
            common_pb2.GetMetricsRequest(scope="collector"),
            timeout=self.default_timeout,
        )
        values = {item.name: int(item.value) for item in metrics.metrics}
        return CollectorRuntimeStatus(
            running=health.health == "RUNNING",
            device_count=len(devices),
            devices_connected=sum(item.connected for item in devices),
            points_collected=values.get("points_total", 0),
            points_routed=values.get("points_routed", 0),
            points_dropped=values.get("points_dropped", 0),
            acquisitions=[],
        )

    async def metrics_snapshot(self) -> MetricsSnapshot:
        raw = await self._common.GetMetrics(
            common_pb2.GetMetricsRequest(scope="collector"),
            timeout=self.default_timeout,
        )
        result = await self._common.GetEvents(
            common_pb2.GetEventsRequest(page_size=500),
            timeout=self.default_timeout,
        )
        counts = {item.name: int(item.value) for item in raw.metrics}
        return MetricsSnapshot(
            counters=CounterSnapshot(
                points_total=counts.get("points_total", 0),
                points_bad=counts.get("points_bad", 0),
                acquisition_runs=counts.get("acquisition_runs", 0),
                acquisition_failures=counts.get("acquisition_failures", 0),
                acquisition_partial=counts.get("acquisition_partial", 0),
                missed_cycles=counts.get("missed_cycles", 0),
                poll_overruns=counts.get("poll_overruns", 0),
                connect_failures=counts.get("connect_failures", 0),
                reconnects=counts.get("reconnects", 0),
            ),
            events=[
                MonitoringEvent(
                    timestamp=item.timestamp.ToDatetime(tzinfo=UTC),
                    kind=item.error_code,
                    object=item.source,
                    message=item.message,
                )
                for item in result.events
            ],
        )

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
                    connected=item.actual_state.upper() == "RUNNING",
                    last_seen=None,
                    consecutive_failures=0,
                    last_error=None,
                )
                for item in response.devices
            )
            token = response.next_page_token
            if not token:
                return rows

    async def _tasks(self) -> list[pb.Task]:
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
        return [_summary(item) for item in await self._tasks()]

    async def list_task_instances(self) -> list[CollectorTaskInstance]:
        return [_instance(item) for item in await self._tasks()]

    @staticmethod
    def _admin_metadata() -> tuple[tuple[str, str], ...]:
        token = os.getenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN")
        if not token:
            raise PermissionError("Collector task control requires admin credentials")
        return (("x-windhub-admin-token", token),)

    async def _set_task(self, device_id: str, *, start: bool) -> pb.Task:
        metadata = self._admin_metadata()
        previous = await self._collector.GetTask(
            common_pb2.ObjectId(id=device_id),
            timeout=self.default_timeout,
        )
        request = common_pb2.OperationRequest(
            id=device_id,
            expected_revision=previous.task.revision,
        )
        operation = self._collector.StartTask if start else self._collector.StopTask
        try:
            result = await operation(
                request,
                metadata=metadata,
                timeout=self.default_timeout,
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() in {
                grpc.StatusCode.ABORTED,
                grpc.StatusCode.FAILED_PRECONDITION,
            }:
                raise CollectorTaskConflictError(exc.details() or exc.code().name) from exc
            raise
        if not result.accepted:
            raise RuntimeError(result.message or result.error_code or "task operation rejected")
        updated = await self._collector.GetTask(
            common_pb2.ObjectId(id=device_id),
            timeout=self.default_timeout,
        )
        return updated.task

    async def start_task(self, device_id: str) -> CollectorTaskSummary:
        return _summary(await self._set_task(device_id, start=True))

    async def stop_task(self, device_id: str) -> CollectorTaskSummary:
        return _summary(await self._set_task(device_id, start=False))

    async def start_task_instance(self, device_id: str) -> CollectorTaskInstance:
        return _instance(await self._set_task(device_id, start=True))

    async def stop_task_instance(self, device_id: str) -> CollectorTaskInstance:
        return _instance(await self._set_task(device_id, start=False))
