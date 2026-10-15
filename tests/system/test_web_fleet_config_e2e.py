"""30 个真实协议服务映射至 Web Server 管理的 Core 配置与 HTTP API。

使用现有三协议真实 fixture，并将所有设备映射到同一配置快照。API 以独立
uvicorn 进程对外服务；Worker 不可达时必须安全拒绝启动，但读模型仍可用。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import yaml

from scripts.generate_virtual_site import generate_site
from tests.support.process import CollectorProcess, free_port, start_web_server
from tests.system.test_web_protocol_fleet_e2e import (  # noqa: F401
    ProtocolFleet,
    protocol_fleet,
)
from web_server.application.config.service import ConfigService

pytestmark = [pytest.mark.asyncio(loop_scope="module"), pytest.mark.system]

_FAMILY = ("modbus", "ads", "iec104")


def build_fleet_config(destination: Path, fleet: ProtocolFleet) -> Path:
    """复用 scripts 生成的 Core 正式风场配置，仅更新独立测试端口。"""
    generate_site(destination, count=10)
    path = destination / "devices.yaml"
    with path.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream)
    for family in _FAMILY:
        endpoints = fleet.endpoints()[family]
        for index, (host, port) in enumerate(endpoints):
            device = document["devices"][f"{family}-{index:02d}"]
            device["endpoint"]["host"] = host
            device["endpoint"]["port"] = port if family != "ads" else 801
            if family == "ads":
                device["protocol_options"]["target_net_id"] = f"{host}.2.1"
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)
    ConfigService.load_directory(destination)
    return destination


@pytest.fixture(scope="module")
def fleet_config_dir(
    tmp_path_factory: pytest.TempPathFactory,
    protocol_fleet: ProtocolFleet,  # noqa: F811  # pytest fixture 注入
) -> Path:
    return build_fleet_config(tmp_path_factory.mktemp("web-fleet") / "site", protocol_fleet)


def test_core_can_load_all_30_device_endpoints(fleet_config_dir: Path) -> None:
    snapshot = ConfigService.load_directory(fleet_config_dir)
    assert len(snapshot.site.devices) == 30
    assert len(snapshot.tasks) == 3
    assert len(snapshot.sinks) >= 1


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def web_server(
    fleet_config_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[tuple[str, CollectorProcess]]:
    """真正启动 wind-hub-server 进程，并记录失败时可检查的日志。"""
    port = free_port()
    collector_port = free_port()
    commander_port = free_port()
    logs = tmp_path_factory.mktemp("web-fleet-server") / "server.log"
    proc = start_web_server(
        fleet_config_dir,
        http_port=port,
        collectors=(f"collector=127.0.0.1:{collector_port}",),
        commander=f"127.0.0.1:{commander_port}",
        log_dir=logs.parent,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=url, timeout=4) as client:
            for _ in range(100):
                if proc.proc.poll() is not None:
                    raise AssertionError(f"server failed: {proc.read_log()}")
                try:
                    response = await client.get("/openapi.json")
                    if response.status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(f"server never became ready: {proc.read_log()}")
        yield url, proc
    finally:
        if proc.is_running():
            proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.proc.wait), timeout=15)
        except TimeoutError:
            proc.kill_tree()
            await asyncio.to_thread(proc.proc.wait)
        finally:
            proc.close_log()


async def test_live_http_exposes_30_protocol_devices(
    web_server: tuple[str, CollectorProcess],
) -> None:
    url, process = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.get("/api/v1/devices", params={"page_size": 100})
        assert response.status_code == 200, response.text
        data = response.json()
        items = data["items"] if isinstance(data, dict) else data
        assert len(items) == 30
        assert {item["device_id"].split("-")[0] for item in items} == set(_FAMILY)
        assert process.proc.poll() is None


@pytest.mark.parametrize("resource", ["settings", "definitions", "sinks", "tasks", "config/files"])
async def test_live_read_models_remain_available_during_worker_outage(
    web_server: tuple[str, CollectorProcess],
    resource: str,
) -> None:
    url, _ = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.get(f"/api/v1/{resource}")
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("task_id", ["e2e-modbus", "e2e-ads", "e2e-iec104"])
async def test_task_start_fails_closed_when_collector_is_unreachable(
    web_server: tuple[str, CollectorProcess],
    task_id: str,
) -> None:
    url, process = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.post(f"/api/v1/tasks/{task_id}/start")
        assert response.status_code in {400, 409, 422, 503}
        assert process.poll() is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/config/files/../../etc/passwd",
        "/api/v1/config/files/unknown.yaml",
        "/api/v1/tasks/%00/start",
        "/api/v1/sinks/unknown-sink/verify",
    ],
)
async def test_path_traversal_and_invalid_targets_are_rejected(
    web_server: tuple[str, CollectorProcess],
    path: str,
) -> None:
    url, _ = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.get(path) if "config/files" in path else await client.post(path)
        assert response.status_code in {400, 404, 405, 409, 422, 503}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"site_id": ""},
        {"site_id": "wind", "api_port": -1},
        {"site_id": "wind", "ads_username": "unsafe"},
    ],
)
async def test_invalid_settings_payloads_are_rejected(
    web_server: tuple[str, CollectorProcess],
    body: dict[str, object],
) -> None:
    url, _ = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.put("/api/v1/settings", json=body)
        assert response.status_code in {400, 409, 422}


@pytest.mark.parametrize(
    "content",
    [
        "not: [valid",
        "interfaces:\n  api:\n    port: 9\n",
        "site:\n  site_id: wrong\nsite:\n  site_id: duplicate\n",
    ],
)
async def test_malformed_configuration_fails_without_mutating_live_snapshot(
    web_server: tuple[str, CollectorProcess],
    content: str,
) -> None:
    url, process = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        before = (await client.get("/api/v1/settings")).json()
        response = await client.post(
            "/api/v1/config/validate",
            json={"name": "system.yaml", "content": content},
        )
        assert response.status_code in {200, 400, 422}
        if response.status_code == 200:
            assert not response.json().get("valid", False)
        after = (await client.get("/api/v1/settings")).json()
        assert after == before
        assert process.poll() is None


async def test_parallel_invalid_requests_do_not_crash_http_server(
    web_server: tuple[str, CollectorProcess],
) -> None:
    url, process = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        requests = [
            client.post(
                "/api/v1/config/validate",
                json={"name": "../etc/passwd", "content": "a: 1"},
            )
            for _ in range(30)
        ]
        results = await asyncio.gather(*requests)
        assert all(response.status_code < 500 for response in results)
        assert (await client.get("/openapi.json")).status_code == 200
        assert process.poll() is None
