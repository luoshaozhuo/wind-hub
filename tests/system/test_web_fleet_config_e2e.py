"""30 个真实协议服务映射至 Web Server 管理的 Core 配置与 HTTP API。

使用现有三协议真实 fixture，并将所有设备映射到同一配置快照。API 以独立
uvicorn 进程对外服务；Worker 不可达时必须安全拒绝启动，但读模型仍可用。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import yaml

from tests.support.process import free_port
from tests.system.test_web_protocol_fleet_e2e import ProtocolFleet, protocol_fleet
from web_server.application.config.service import ConfigService

pytestmark = [pytest.mark.asyncio(loop_scope="module"), pytest.mark.system]

_ROOT = Path(__file__).resolve().parents[2]
_FAMILY = ("modbus", "ads", "iec104")


def _load(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"invalid test YAML: {path}")
    return value


def _write(path: Path, data: dict[str, object]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)


def build_fleet_config(destination: Path, fleet: ProtocolFleet) -> Path:
    """以正式 Core 示例为基线生成 30 台设备、三协议点表与采集 Task。"""
    shutil.copytree(_ROOT / "configs" / "example_modbus", destination)
    system = _load(destination / "system.yaml")
    system.pop("interfaces", None)
    system["ads"] = {
        "local_ams_net_id": "127.0.0.1.1.1",
        "local_ip": "127.0.0.1",
    }
    _write(destination / "system.yaml", system)

    models = _load(destination / "device_models.yaml")
    models["device_models"]["e2e_ads"] = {
        "device_type_id": "turbine",
        "point_table_id": "e2e_ads",
        "protocol": "ads",
        "endpoint_defaults": {"port": 801},
        "protocol_options": {"twincat_version": "2"},
    }
    models["device_models"]["e2e_iec104"] = {
        "device_type_id": "turbine",
        "point_table_id": "e2e_iec104",
        "protocol": "iec104",
        "endpoint_defaults": {"port": 2404},
        "protocol_options": {"common_addr": 1},
    }
    _write(destination / "device_models.yaml", models)

    business = _load(destination / "business_points.yaml")
    business["business_points"]["e2e_float"] = {
        "data_type": "float32",
        "unit_id": "none",
    }
    _write(destination / "business_points.yaml", business)

    points = _load(destination / "points.yaml")
    base = {
        "business_point_id": "e2e_float",
        "point_groups": ["all"],
        "data_type": "float32",
        "scale": 1.0,
        "offset": 0.0,
        "unit_id": "none",
    }
    points["point_tables"]["e2e_ads"] = {
        "protocol": "ads",
        "points": {
            "p1": {**base, "address": {"symbol": "MAIN.var0"}},
        },
    }
    points["point_tables"]["e2e_iec104"] = {
        "protocol": "iec104",
        "points": {
            "p1": {**base, "address": {"ioa": 100, "type": "M_ME_NC_1"}},
        },
    }
    _write(destination / "points.yaml", points)

    devices: dict[str, object] = {}
    groups: dict[str, object] = {}
    for family in _FAMILY:
        group = f"e2e_{family}"
        groups[group] = {"name": group}
        for index, (host, port) in enumerate(fleet.endpoints()[family]):
            device: dict[str, object] = {
                "model_id": {
                    "modbus": "wtg_modbus_site",
                    "ads": "e2e_ads",
                    "iec104": "e2e_iec104",
                }[family],
                "device_group_ids": [group],
                "endpoint": {"host": host, "port": port if family != "ads" else 801},
                "enabled": True,
            }
            if family == "modbus":
                device["protocol_options"] = {"slave_id": 1}
            elif family == "ads":
                device["protocol_options"] = {"target_net_id": "127.0.0.1.2.1"}
            devices[f"{family}-{index:02d}"] = device
    _write(destination / "devices.yaml", {"devices": devices, "device_groups": groups})
    tasks = {
        f"e2e-{family}": {
            "point_group": "all",
            "device_group_id": f"e2e_{family}",
            "interval": 2.0,
            "sink_ids": ["file_archive"],
            "enabled": True,
        }
        for family in _FAMILY
    }
    _write(destination / "tasks.yaml", {"tasks": tasks})
    sinks = _load(destination / "sinks.yaml")
    sinks["sinks"]["file_archive"]["connection"]["path"] = str(
        destination / "e2e.csv"
    )
    _write(destination / "sinks.yaml", sinks)
    return destination


@pytest.fixture(scope="module")
def fleet_config_dir(
    tmp_path_factory: pytest.TempPathFactory,
    protocol_fleet: ProtocolFleet,
) -> Path:
    return build_fleet_config(tmp_path_factory.mktemp("web-fleet") / "site", protocol_fleet)


def test_core_can_load_all_30_device_endpoints(fleet_config_dir: Path) -> None:
    snapshot = ConfigService.load_directory(fleet_config_dir)
    assert len(snapshot.site.devices) == 30
    assert len(snapshot.tasks) == 3
    assert len(snapshot.sinks) >= 1


@pytest.fixture(scope="module")
async def web_server(
    fleet_config_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[tuple[str, subprocess.Popen[str]]]:
    """真正启动 wind-hub-server 进程，并记录失败时可检查的日志。"""
    port = free_port()
    collector_port = free_port()
    commander_port = free_port()
    logs = tmp_path_factory.mktemp("web-fleet-server") / "server.log"
    with logs.open("w", encoding="utf-8") as output:
        proc = subprocess.Popen(
            [
                "wind-hub-server",
                "--config", str(fleet_config_dir),
                "--host", "127.0.0.1",
                "--port", str(port),
                "--collector", f"collector=127.0.0.1:{collector_port}",
                "--commander", f"127.0.0.1:{commander_port}",
                "--worker-probe-interval", "60",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(base_url=url, timeout=4) as client:
                for _ in range(100):
                    if proc.poll() is not None:
                        raise AssertionError(f"server failed: {logs.read_text()}")
                    try:
                        response = await client.get("/openapi.json")
                        if response.status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError(f"server never became ready: {logs.read_text()}")
            yield url, proc
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=15)
            except TimeoutError:
                proc.kill()
                await asyncio.to_thread(proc.wait)


async def test_live_http_exposes_30_protocol_devices(
    web_server: tuple[str, subprocess.Popen[str]],
) -> None:
    url, process = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.get("/api/v1/devices", params={"page_size": 100})
        assert response.status_code == 200, response.text
        data = response.json()
        items = data["items"] if isinstance(data, dict) else data
        assert len(items) == 30
        assert {item["device_id"].split("-")[0] for item in items} == set(_FAMILY)
        assert process.poll() is None


@pytest.mark.parametrize("resource", ["settings", "definitions", "sinks", "tasks", "config/files"])
async def test_live_read_models_remain_available_during_worker_outage(
    web_server: tuple[str, subprocess.Popen[str]], resource: str,
) -> None:
    url, _ = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.get(f"/api/v1/{resource}")
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("task_id", ["e2e-modbus", "e2e-ads", "e2e-iec104"])
async def test_task_start_fails_closed_when_collector_is_unreachable(
    web_server: tuple[str, subprocess.Popen[str]], task_id: str,
) -> None:
    url, process = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.post(f"/api/v1/tasks/{task_id}/start")
        assert response.status_code in {400, 409, 422, 503}
        assert process.poll() is None


@pytest.mark.parametrize("path", [
    "/api/v1/config/files/../../etc/passwd",
    "/api/v1/config/files/unknown.yaml",
    "/api/v1/tasks/%00/start",
    "/api/v1/sinks/unknown-sink/verify",
])
async def test_path_traversal_and_invalid_targets_are_rejected(
    web_server: tuple[str, subprocess.Popen[str]], path: str,
) -> None:
    url, _ = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.get(path) if "config/files" in path else await client.post(path)
        assert response.status_code in {400, 404, 405, 409, 422, 503}


@pytest.mark.parametrize("body", [
    {},
    {"site_id": ""},
    {"site_id": "wind", "api_port": -1},
    {"site_id": "wind", "ads_username": "unsafe"},
])
async def test_invalid_settings_payloads_are_rejected(
    web_server: tuple[str, subprocess.Popen[str]], body: dict[str, object],
) -> None:
    url, _ = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        response = await client.put("/api/v1/settings", json=body)
        assert response.status_code in {400, 409, 422}


@pytest.mark.parametrize("content", [
    "not: [valid",
    "interfaces:\n  api:\n    port: 9\n",
    "site:\n  site_id: wrong\nsite:\n  site_id: duplicate\n",
])
async def test_malformed_configuration_fails_without_mutating_live_snapshot(
    web_server: tuple[str, subprocess.Popen[str]], content: str,
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
    web_server: tuple[str, subprocess.Popen[str]],
) -> None:
    url, process = web_server
    async with httpx.AsyncClient(base_url=url, timeout=8) as client:
        requests = [
            client.post("/api/v1/config/validate", json={"name": "../etc/passwd", "content": "a: 1"})
            for _ in range(30)
        ]
        results = await asyncio.gather(*requests)
        assert all(response.status_code < 500 for response in results)
        assert (await client.get("/openapi.json")).status_code == 200
        assert process.poll() is None
