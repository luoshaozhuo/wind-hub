"""wind-hub-ctl ↔ Collector gRPC 只读诊断闭环测试。"""

from __future__ import annotations

import grpc
import pytest

from wind_hub_collector.adapter.inbound.grpc.server import build_grpc_server
from wind_hub_collector.application.runtime.collector_identity import CollectorIdentity
from wind_hub_collector.application.runtime.task_instance import TaskInstanceState
from wind_hub_collector.application.usecase.query import SystemStatus
from wind_hub_collector.application.usecase.task import TaskInstanceDetail, TaskSummary
from wind_hub_collector.domain.model.device import DeviceInfo
from wind_hub_core.rpc import collector_pb2 as pb
from wind_hub_core.rpc import collector_pb2_grpc as pb_grpc
from wind_hub_ctl.client import CollectorClient


def _task_summary(task_id: str) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        device="d1",
        device_group=None,
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        enabled=True,
        runtime_state="running",
        instance_count=1,
        running_instances=1,
        stopped_instances=0,
        failed_instances=0,
    )


def _instance_detail(instance_id: str) -> TaskInstanceDetail:
    return TaskInstanceDetail(
        instance_id=instance_id,
        task_id="t1",
        device_id="d1",
        point_group="fast",
        interval=1.0,
        targets=["archive"],
        state=TaskInstanceState.RUNNING,
    )


class _CollectorRuntimeCore:
    running = True
    sinks: dict[str, object] = {}

    def health(self) -> dict[str, object]:
        return {}

    def sink_queue_depths(self) -> dict[str, int]:
        return {}

    def task_definitions(self) -> dict[str, object]:
        return {"t1": object()}


class _Query:
    async def status(self) -> SystemStatus:
        return SystemStatus(
            running=True,
            device_count=1,
            sink_count=0,
            devices_connected=1,
        )

    async def list_devices(self) -> list[DeviceInfo]:
        return [DeviceInfo(device_id="d1", protocol="modbus", connected=True)]


class _Tasks:
    async def list_task_summaries(self) -> list[TaskSummary]:
        return [_task_summary("t1")]

    async def get_task_summary(self, task_id: str) -> TaskSummary:
        return _task_summary(task_id)

    async def start_task(self, task_id: str) -> TaskSummary:
        return await self.get_task_summary(task_id)

    async def list_instances(self) -> list[TaskInstanceDetail]:
        return [_instance_detail("t1:d1")]

    async def get_instance(self, instance_id: str) -> TaskInstanceDetail:
        return _instance_detail(instance_id)


class _Config:
    config_hash = "hash-current"
    active_revision = "rev-current"
    prepared_revision = None
    prepared_hash = None


class _Metrics:
    def snapshot(self) -> dict[str, object]:
        return {
            "counters": {
                "points_total": 0,
                "points_bad": 0,
                "acquisition_runs": 0,
                "acquisition_failures": 0,
                "acquisition_partial": 0,
                "missed_cycles": 0,
                "poll_overruns": 0,
                "connect_failures": 0,
                "reconnects": 0,
            },
            "device_connect_failures": {},
            "device_reconnects": {},
            "events": [],
        }


class _CollectorApp:
    runtime = _CollectorRuntimeCore()
    query = _Query()
    tasks = _Tasks()
    config = _Config()
    metrics_state = _Metrics()


@pytest.mark.asyncio
async def test_ctl_collector_read_only_grpc_roundtrip() -> None:
    identity = CollectorIdentity(
        collector_id="collector-test",
        boot_id="boot-test",
        config_hash="hash-test",
    )
    server = build_grpc_server(
        _CollectorApp(),  # type: ignore[arg-type]
        identity,
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    try:
        async with CollectorClient(server.endpoint) as client:
            info = await client.info()
            assert info["collector_id"] == "collector-test"
            assert info["runtime_running"] is True

            status = await client.status()
            assert status["device_count"] == 1

            devices = await client.devices()
            assert devices["items"][0]["device_id"] == "d1"

            tasks = await client.tasks()
            assert tasks["items"][0]["task_id"] == "t1"

            task = await client.task("t1")
            assert task["task_id"] == "t1"

            instances = await client.task_instances()
            assert instances["items"][0]["instance_id"] == "t1:d1"

            instance = await client.task_instance("t1:d1")
            assert instance["instance_id"] == "t1:d1"
    finally:
        await server.stop(grace=0)


@pytest.mark.asyncio
async def test_collector_start_requires_current_task_placement() -> None:
    identity = CollectorIdentity(
        collector_id="collector-test",
        boot_id="boot-test",
        config_hash="hash-test",
    )
    server = build_grpc_server(
        _CollectorApp(),  # type: ignore[arg-type]
        identity,
        host="127.0.0.1",
        port=0,
    )
    await server.start()
    channel = grpc.aio.insecure_channel(server.endpoint)
    stub = pb_grpc.CollectorControlServiceStub(channel)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as missing:
            await stub.StartTask(
                pb.TaskStartRequest(task_id="t1", placement_generation=1)
            )
        assert missing.value.code() is grpc.StatusCode.FAILED_PRECONDITION

        applied = await stub.ApplyTaskPlacement(
            pb.TaskPlacementSnapshotRequest(
                worker_id="collector-test",
                generation=1,
                task_ids=["t1"],
            )
        )
        assert applied.success is True
        assert applied.generation == 1
        assert applied.task_count == 1

        started = await stub.StartTask(
            pb.TaskStartRequest(task_id="t1", placement_generation=1)
        )
        assert started.task_id == "t1"

        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await stub.StartTask(
                pb.TaskStartRequest(task_id="other", placement_generation=1)
            )
        assert denied.value.code() is grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()
        await server.stop(grace=0)
