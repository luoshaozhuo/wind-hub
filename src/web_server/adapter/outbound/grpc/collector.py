"""Outbound Collector client for the authoritative CollectorService contract."""

from __future__ import annotations

import os

import grpc

from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc as pb_grpc
from contracts import common_pb2
from web_server.adapter.outbound.grpc.common import GrpcClientBase
from web_server.application.port.monitoring import MetricsSnapshot
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

    async def config_status(self) -> CollectorInfo:
        """The current Collector contract has no identity or config-status RPC."""
        raise NotImplementedError("Collector configuration status is not in collector.proto")

    async def runtime_status(self) -> CollectorRuntimeStatus:
        """Do not fabricate missing aggregate counters as zeros."""
        raise NotImplementedError("Collector runtime metrics are not in collector.proto")

    async def metrics_snapshot(self) -> MetricsSnapshot:
        """Do not silently return empty metrics when no metrics RPC exists."""
        raise NotImplementedError("Collector metrics are not in collector.proto")

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
