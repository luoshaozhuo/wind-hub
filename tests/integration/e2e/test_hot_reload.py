"""端到端测试 —— 热重载：新增设备（unit 2）后引擎在运行中接入并采集。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient

from wind_hub.assembly import AssembledRuntime


def _append_device_and_points(config_dir: Path) -> None:
    """向设备/点表追加 ``modbus-2``（unit 2），供热重载接入。"""
    devices_path = config_dir / "devices.yaml"
    devices = yaml.safe_load(devices_path.read_text(encoding="utf-8"))
    devices["devices"].append(
        {
            "device_id": "modbus-2",
            "protocol": "modbus",
            "endpoint": {
                "host": "127.0.0.1",
                "port": 15020,
                "extensions": {"unit_id": 2, "timeout": 2.0, "reconnect_max_retries": 20},
            },
            "polling": [{"group": "telemetry", "interval": 0.2}],
            "enabled": True,
        }
    )
    devices_path.write_text(yaml.safe_dump(devices, sort_keys=False), encoding="utf-8")

    points_path = config_dir / "points.yaml"
    points = yaml.safe_load(points_path.read_text(encoding="utf-8"))
    points["points"].extend(
        [
            {
                "point_id": "rotor.speed",
                "device_id": "modbus-2",
                "address": {"register_type": "holding", "address": 100},
                "data_type": "float32",
            },
            {
                "point_id": "gen.power",
                "device_id": "modbus-2",
                "address": {"register_type": "holding", "address": 102},
                "data_type": "float32",
            },
        ]
    )
    points_path.write_text(yaml.safe_dump(points, sort_keys=False), encoding="utf-8")


async def test_hot_reload_adds_device(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
    config_dir: Path,
) -> None:
    """重载配置新增 modbus-2（unit 2），设备列表从 2 变成 3。"""
    assert len((await api_client.get("/devices")).json()) == 2

    _append_device_and_points(config_dir)

    resp = await api_client.post("/config/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["devices_added"] == ["modbus-2"]

    devices = (await api_client.get("/devices")).json()
    ids = {d["device_id"] for d in devices}
    assert ids == {"modbus-1", "iec104-1", "modbus-2"}


async def test_hot_reload_new_device_collects(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
    config_dir: Path,
) -> None:
    """新增设备（unit 2）接入后能被实时读取到正确值。"""
    _append_device_and_points(config_dir)
    resp = await api_client.post("/config/reload")
    assert resp.json()["success"] is True

    # unit 2 的 rotor.speed 预设为 900.5
    deadline = asyncio.get_event_loop().time() + 5.0
    while True:
        read = await api_client.get("/points/modbus-2/rotor.speed")
        if read.status_code == 200:
            assert read.json()["value"] == pytest.approx(900.5)
            return
        assert asyncio.get_event_loop().time() < deadline, "modbus-2 not readable after reload"
        await asyncio.sleep(0.1)
