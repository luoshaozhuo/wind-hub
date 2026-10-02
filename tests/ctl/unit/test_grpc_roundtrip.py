"""wind-hub-ctl ↔ Collector gRPC 最小闭环测试。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from wind_hub.adapter.inbound.grpc.server import build_grpc_server
from wind_hub.application.runtime.collector_identity import CollectorIdentity
from wind_hub_ctl.client import CollectorClient


@dataclass
class _Payload:
    data: dict[str, Any]

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return self.data


class _RuntimeCore:
    running = True


class _Query:
    async def status(self) -> _Payload:
        return _Payload({"running": True, "device_count": 1, "sink_count": 1})

    async def list_devices(self) -> list[_Payload]:
        return [_Payload({"device_id": "d1", "protocol": "modbus", "connected": True})]

    async def read_point(self, device_id: str, point_id: str) -> _Payload:
        return _Payload(
            {
                "device_id": device_id,
                "point_id": point_id,
                "value": 12.5,
                "quality": "good",
                "source": "test",
            }
        )


class _Tasks:
    async def list_task_summaries(self) -> list[_Payload]:
        return [_Payload({"task_id": "t1", "runtime_state": "running"})]

    async def get_task_summary(self, task_id: str) -> _Payload:
        return _Payload({"task_id": task_id, "runtime_state": "running"})

    async def start_task(self, task_id: str) -> _Payload:
        return _Payload({"task_id": task_id, "runtime_state": "running"})

    async def stop_task(self, task_id: str) -> _Payload:
        return _Payload({"task_id": task_id, "runtime_state": "stopped"})

    async def list_instances(self) -> list[_Payload]:
        return [_Payload({"instance_id": "t1:d1", "state": "running"})]

    async def get_instance(self, instance_id: str) -> _Payload:
        return _Payload({"instance_id": instance_id, "state": "running"})

    async def start_instance(self, instance_id: str) -> _Payload:
        return _Payload({"instance_id": instance_id, "state": "running"})

    async def stop_instance(self, instance_id: str) -> _Payload:
        return _Payload({"instance_id": instance_id, "state": "stopped"})

    async def start_all_instances(self) -> _Payload:
        return _Payload({"success": True, "operation": "start_all"})

    async def stop_all_instances(self) -> _Payload:
        return _Payload({"success": True, "operation": "stop_all"})


class _Config:
    config_hash = "hash-current"

    async def reload(self) -> _Payload:
        return _Payload(
            {
                "success": True,
                "diff": {},
                "errors": [],
                "duration_ms": 1.0,
            }
        )


class _Command:
    async def send(self, command: Any) -> _Payload:
        return _Payload(
            {
                "command_id": command.command_id,
                "success": True,
                "error": None,
            }
        )


class _AssembledRuntime:
    runtime = _RuntimeCore()
    query = _Query()
    tasks = _Tasks()
    command = _Command()
    config = _Config()


@pytest.mark.asyncio
async def test_ctl_collector_grpc_roundtrip() -> None:
    identity = CollectorIdentity(
        collector_id="collector-test",
        boot_id="boot-test",
        config_hash="hash-test",
    )
    server = build_grpc_server(
        _AssembledRuntime(),  # type: ignore[arg-type]
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
            assert info["config_hash"] == "hash-current"
            assert info["boot_config_hash"] == "hash-test"

            status = await client.status()
            assert status["device_count"] == 1

            devices = await client.devices()
            assert devices["items"][0]["device_id"] == "d1"

            value = await client.read("d1", "p1")
            assert value["value"] == 12.5

            tasks = await client.tasks()
            assert tasks["items"][0]["task_id"] == "t1"

            task = await client.task("t1")
            assert task["task_id"] == "t1"

            stopped_task = await client.stop_task("t1")
            assert stopped_task["runtime_state"] == "stopped"

            started_task = await client.start_task("t1")
            assert started_task["runtime_state"] == "running"

            instances = await client.task_instances()
            assert instances["items"][0]["instance_id"] == "t1:d1"

            instance = await client.task_instance("t1:d1")
            assert instance["instance_id"] == "t1:d1"

            stopped = await client.stop_task_instance("t1:d1")
            assert stopped["state"] == "stopped"

            started = await client.start_task_instance("t1:d1")
            assert started["state"] == "running"

            assert (await client.stop_all())["success"] is True
            assert (await client.start_all())["success"] is True

            reloaded = await client.reload()
            assert reloaded["success"] is True

            written = await client.write("d1", "p1", 10.0)
            assert written["success"] is True
    finally:
        await server.stop(grace=0)
