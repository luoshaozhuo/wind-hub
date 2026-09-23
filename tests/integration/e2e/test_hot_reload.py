"""端到端测试 —— 热重载：devices/tasks diff 驱动设备接入与 Task Instance 增删。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient

from wind_hub.assembly import AssembledRuntime


def _read_yaml(config_dir: Path, name: str) -> dict:
    return yaml.safe_load((config_dir / name).read_text(encoding="utf-8"))


def _write_yaml(config_dir: Path, name: str, data: dict) -> None:
    (config_dir / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _append_device_and_task(config_dir: Path) -> None:
    """追加 ``modbus-2``（unit 2）设备与对应采集 Task，供热重载接入。

    ``modbus-2`` 与 ``modbus-1`` 同型号，直接共享 ``modbus`` 点表；
    新模型下采集节奏由 Task（``interval``/``targets``）而非设备 polling 决定。
    """
    devices = _read_yaml(config_dir, "devices.yaml")
    devices["devices"].append(
        {
            "device_id": "modbus-2",
            "protocol": "modbus",
            "point_table": "modbus",
            "endpoint": {
                "host": "127.0.0.1",
                "port": 15020,
                "extensions": {"unit_id": 2, "timeout": 2.0, "reconnect_max_retries": 20, "word_order": "big_endian"},
            },
            "enabled": True,
        }
    )
    _write_yaml(config_dir, "devices.yaml", devices)

    tasks = _read_yaml(config_dir, "tasks.yaml")
    tasks["tasks"].append(
        {
            "task_id": "modbus-2-telemetry",
            "device": "modbus-2",
            "point_group": "telemetry",
            "interval": 0.2,
            "targets": [{"sink": "null_sink"}],
        }
    )
    _write_yaml(config_dir, "tasks.yaml", tasks)


async def test_hot_reload_adds_device_and_task(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
    config_dir: Path,
) -> None:
    """重载新增 modbus-2 与其 Task，devices/tasks diff 字段如实上报（无 routing 字段）。"""
    assert len((await api_client.get("/devices")).json()) == 2

    _append_device_and_task(config_dir)

    resp = await api_client.post("/config/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["devices_added"] == ["modbus-2"]
    assert body["tasks_added"] == ["modbus-2-telemetry"]
    assert "routing_rebuilt" not in body

    devices = (await api_client.get("/devices")).json()
    ids = {d["device_id"] for d in devices}
    assert ids == {"modbus-1", "iec104-1", "modbus-2"}

    # 新 Task 展开为新实例——按启动语义以 STOPPED 注册。
    instances = (await api_client.get("/tasks/instances")).json()
    by_id = {i["instance_id"]: i for i in instances}
    assert "modbus-2-telemetry:modbus-2" in by_id
    assert by_id["modbus-2-telemetry:modbus-2"]["state"] == "stopped"


async def test_hot_reload_new_device_collects(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
    config_dir: Path,
) -> None:
    """新增设备（unit 2）接入并启动其实例后，能实时读到正确值。"""
    _append_device_and_task(config_dir)
    resp = await api_client.post("/config/reload")
    assert resp.json()["success"] is True

    # unit 2 的 rotor.speed 预设为 900.5（/points 为实时读，不依赖采集循环）
    deadline = asyncio.get_event_loop().time() + 5.0
    while True:
        read = await api_client.get("/points/modbus-2/rotor.speed")
        if read.status_code == 200:
            assert read.json()["value"] == pytest.approx(900.5)
            return
        assert asyncio.get_event_loop().time() < deadline, "modbus-2 not readable after reload"
        await asyncio.sleep(0.1)


async def test_hot_reload_removed_task_unregisters_instances(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
    config_dir: Path,
) -> None:
    """删除 Task 后其全部实例被注销（运行中协程一并取消）。"""
    before = (await api_client.get("/tasks/instances")).json()
    assert "iec104-telemetry:iec104-1" in {i["instance_id"] for i in before}

    tasks = _read_yaml(config_dir, "tasks.yaml")
    tasks["tasks"] = [t for t in tasks["tasks"] if t["task_id"] != "iec104-telemetry"]
    _write_yaml(config_dir, "tasks.yaml", tasks)

    resp = await api_client.post("/config/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["tasks_removed"] == ["iec104-telemetry"]

    after = (await api_client.get("/tasks/instances")).json()
    assert "iec104-telemetry:iec104-1" not in {i["instance_id"] for i in after}


async def test_hot_reload_device_group_membership_changes_instances(
    runtime: AssembledRuntime,
    api_client: AsyncClient,
    config_dir: Path,
) -> None:
    """device_group Task：设备加入分组展开新实例，移出分组注销实例。

    loader 校验要求 device_group Task 至少匹配一台设备——因此两台设备先入
    组，再移出其中一台（保留 iec104-1 使配置仍合法）。
    """
    # 两台设备加入 wtg 分组，并新增一个 device_group Task。
    devices = _read_yaml(config_dir, "devices.yaml")
    for dev in devices["devices"]:
        dev["device_group"] = "wtg"
    _write_yaml(config_dir, "devices.yaml", devices)

    tasks = _read_yaml(config_dir, "tasks.yaml")
    tasks["tasks"].append(
        {
            "task_id": "wtg-telemetry",
            "device_group": "wtg",
            "point_group": "telemetry",
            "interval": 0.5,
            "targets": [{"sink": "null_sink"}],
        }
    )
    _write_yaml(config_dir, "tasks.yaml", tasks)

    resp = await api_client.post("/config/reload")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    instances = (await api_client.get("/tasks/instances")).json()
    ids = {i["instance_id"] for i in instances}
    assert "wtg-telemetry:modbus-1" in ids
    assert "wtg-telemetry:iec104-1" in ids

    # modbus-1 移出分组——其 device_group 实例注销，iec104-1 的保留，
    # 设备级实例不受影响。
    devices = _read_yaml(config_dir, "devices.yaml")
    for dev in devices["devices"]:
        if dev["device_id"] == "modbus-1":
            dev.pop("device_group")
    _write_yaml(config_dir, "devices.yaml", devices)

    resp = await api_client.post("/config/reload")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    instances = (await api_client.get("/tasks/instances")).json()
    ids = {i["instance_id"] for i in instances}
    assert "wtg-telemetry:modbus-1" not in ids
    assert "wtg-telemetry:iec104-1" in ids
    assert "modbus-telemetry:modbus-1" in ids
