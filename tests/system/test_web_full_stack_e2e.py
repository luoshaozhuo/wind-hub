"""真实全链路：Core YAML → 三协议各十台 → Collector/Commander → Web Server HTTP。

本套件不使用 MSW/fake gRPC，所有控制服务为独立进程。离线执行即可；
每个用例独立目录、监听端口和故障隔离，结束后优雅关闭全部进程与虚拟从站。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import grpc
import httpx
import pytest
import pytest_asyncio

from contracts import collector_pb2 as pb
from contracts import collector_pb2_grpc as pb_grpc
from scripts.generate_virtual_site import generate_site
from scripts.protocol_servers import run_fleet
from tests.support.process import (
    CollectorProcess,
    free_port,
    free_port_block,
    start_collector,
    start_commander,
    start_web_server,
)
from tests.support.wait import wait_commander_ready, wait_grpc_ready, wait_until

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.asyncio]


@dataclass(slots=True)
class LiveStack:
    url: str
    collector: CollectorProcess
    commander: CollectorProcess
    web: CollectorProcess


def _shutdown(proc: CollectorProcess) -> None:
    try:
        if proc.is_running():
            proc.terminate(timeout=12.0)
    except (TimeoutError, OSError):
        proc.kill_tree()
    finally:
        proc.close_log()


@pytest_asyncio.fixture
async def full_stack(tmp_path: Path) -> AsyncIterator[LiveStack]:
    """同一个 config 下启动 30 个真实从站和三个真实应用进程。"""
    modbus_port = free_port_block(10)
    iec104_port = free_port_block(10, exclude=set(range(modbus_port, modbus_port + 10)))
    config = generate_site(
        tmp_path / "site",
        modbus_base_port=modbus_port,
        iec104_base_port=iec104_port,
    )
    processes: list[CollectorProcess] = []
    async with run_fleet(config, interval=0.25, seed=123):
        try:
            collector = start_collector(config, collector_id="collector", log_dir=tmp_path)
            processes.append(collector)
            commander = start_commander(config, log_dir=tmp_path)
            processes.append(commander)
            await asyncio.gather(
                wait_grpc_ready(collector.grpc_target, timeout=40),
                wait_commander_ready(commander.grpc_target, timeout=40),
            )
            web_port = free_port()
            web = start_web_server(
                config,
                http_port=web_port,
                collectors=(f"collector={collector.grpc_target}",),
                commander=commander.grpc_target,
                log_dir=tmp_path,
            )
            processes.append(web)
            url = f"http://127.0.0.1:{web_port}"

            async def ready() -> bool | None:
                if not web.is_running():
                    raise RuntimeError(web.read_log())
                try:
                    async with httpx.AsyncClient(base_url=url, timeout=2) as client:
                        response = await client.get("/openapi.json")
                    return True if response.status_code == 200 else None
                except httpx.HTTPError:
                    return None

            await wait_until(ready, timeout=40, description="real Web Server HTTP")
            yield LiveStack(url, collector, commander, web)
        finally:
            # 应用先停，再停模拟从站，避免客户端连接遗留。
            for process in reversed(processes):
                await asyncio.to_thread(_shutdown, process)


async def test_full_stack_lists_all_devices_and_workers(full_stack: LiveStack) -> None:
    async with httpx.AsyncClient(base_url=full_stack.url, timeout=10) as client:
        devices = await client.get("/api/v1/devices", params={"page_size": 100})
        assert devices.status_code == 200, devices.text
        assert len(devices.json()["items"]) == 30
        workers = await client.get("/api/v1/workers")
        assert workers.status_code == 200, workers.text
        ids = {entry["worker_id"] for entry in workers.json()}
        assert "collector" in ids
        assert "commander" in ids or "commander-1" in ids


@pytest.mark.parametrize("family", ["modbus", "ads", "iec104"])
async def test_real_task_start_and_stop_via_http(
    full_stack: LiveStack,
    family: str,
) -> None:
    task = f"virtual-{family}"
    async with httpx.AsyncClient(base_url=full_stack.url, timeout=15) as client:
        start = await client.post(f"/api/v1/tasks/{task}/start")
        assert start.status_code == 200, start.text
        instances = await client.get(f"/api/v1/tasks/{task}/instances")
        assert instances.status_code == 200, instances.text
        assert len(instances.json()) == 10
        stop = await client.post(f"/api/v1/tasks/{task}/stop")
        assert stop.status_code == 200, stop.text


async def test_direct_unfenced_collector_start_is_rejected(
    full_stack: LiveStack,
) -> None:
    channel = grpc.aio.insecure_channel(full_stack.collector.grpc_target)
    try:
        stub = pb_grpc.CollectorControlServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await stub.StartTask(
                pb.TaskStartRequest(task_id="virtual-modbus", placement_generation=0),
                timeout=4,
            )
        assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/devices/missing"),
        ("GET", "/api/v1/config/files/missing.yaml"),
        ("POST", "/api/v1/tasks/missing/start"),
        ("POST", "/api/v1/sinks/missing/verify"),
    ],
)
async def test_unknown_resources_are_rejected_without_crashing(
    full_stack: LiveStack,
    method: str,
    path: str,
) -> None:
    async with httpx.AsyncClient(base_url=full_stack.url, timeout=10) as client:
        response = await client.request(method, path)
        assert response.status_code == 404, response.text
        assert full_stack.web.is_running()


async def test_concurrent_invalid_config_does_not_change_live_state(
    full_stack: LiveStack,
) -> None:
    async with httpx.AsyncClient(base_url=full_stack.url, timeout=10) as client:
        before = (await client.get("/api/v1/settings")).json()
        rejected = await asyncio.gather(
            *(
                client.post(
                    "/api/v1/config/apply",
                    json={"name": "system.yaml", "content": "[invalid"},
                )
                for _ in range(20)
            )
        )
        assert all(response.status_code != 200 for response in rejected)
        after = (await client.get("/api/v1/settings")).json()
        assert after == before
        assert full_stack.web.is_running()


async def test_worker_loss_causes_fail_closed_start(
    full_stack: LiveStack,
) -> None:
    await asyncio.to_thread(_shutdown, full_stack.collector)
    async with httpx.AsyncClient(base_url=full_stack.url, timeout=15) as client:
        response = await client.post("/api/v1/tasks/virtual-modbus/start")
        assert response.status_code in {409, 503}, response.text
        assert full_stack.web.is_running()
