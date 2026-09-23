"""系统级集成测试 —— 单配置快照 → 装配 → 真实协议 Server 全链路。

与按主题拆分的既有 e2e（startup / collect / command / hot-reload …）不同，
本文件用**一份自带配置**把完整运行链串起来验证：

- 启动配置单一快照：``assemble()`` 唯一一次 ``load_config`` 的结果同时是
  Runtime 实际配置与 ``ConfigUseCase.current_config`` 的 diff 基线；
- Modbus / IEC104 走真实 localhost socket Server（不 mock ProtocolPort）：
  采集（POLL / SUBSCRIBE+总召）、scale/offset 工程值、命令写入并在
  **Server 侧**确认状态改变；
- Task Definition → Instance 展开（含 device_group 多实例）与
  STOPPED → RUNNING → STOPPED 生命周期（经 TaskUseCase 公共入口）；
- QueryUseCase / CommandUseCase / Web API / ConfigUseCase.reload
  （Task 字段变化 + 点表轻量变化）；
- Sink 为真实 FileSink，落盘到 ``tmp_path`` 并逐行核对内容。

ADS 不在本文件覆盖——受 pyads/TwinCAT 环境约束，由
``tests/integration/adapter/ads/`` 的既有 mock 集成测试保障。
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from tests.fixtures.servers.iec104_control_server import IEC104ControlServer
from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.assembly import AssembledRuntime, assemble, start_runtime
from wind_hub.domain.model.command import Command
from wind_hub.domain.model.point import Quality

from .runtime_helpers import clear_runtime_context, set_runtime_context

pytestmark = pytest.mark.asyncio

# 服务器预设原始值（与 ModbusMockServer 默认布局一致）。
_RAW_ROTOR_SPEED = 1200.5
_RAW_GEN_POWER = 800.0
_RAW_IEC104_ROTOR = 1500.5

# 点表换算参数：modbus rotor.speed = raw * 2 + 10；iec104 rotor.speed = raw * 0.5。
_MODBUS_SCALE, _MODBUS_OFFSET = 2.0, 10.0
_IEC104_SCALE = 0.5

_MODBUS_IID = "modbus-telemetry:modbus-1"
_IEC104_IID = "iec104-telemetry:iec104-1"


def _free_port() -> int:
    """向内核申请一个空闲临时端口（避免硬编码端口冲突）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_yaml(config_dir: Path, name: str, data: dict) -> None:
    (config_dir / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _build_config(config_dir: Path, modbus_port: int, iec104_port: int, sink_path: Path) -> None:
    """写出完整配置目录（system/devices/points/tasks.yaml）。"""
    _write_yaml(
        config_dir,
        "system.yaml",
        {
            "runtime": {
                "connect_timeout": 2.0,
                "read_timeout": 2.0,
                "shutdown_timeout": 5.0,
                "queue_maxsize": 100,
            },
            "sinks": [
                {
                    "name": "file_sink",
                    "type": "file",
                    "enabled": True,
                    "params": {"path": str(sink_path), "format": "jsonl"},
                }
            ],
            "interfaces": {"api": {"enabled": False}, "cli": {"enabled": False}},
        },
    )
    _write_yaml(
        config_dir,
        "devices.yaml",
        {
            "devices": [
                {
                    "device_id": "modbus-1",
                    "protocol": "modbus",
                    "point_table": "modbus",
                    "endpoint": {
                        "host": "127.0.0.1",
                        "port": modbus_port,
                        "extensions": {
                            "unit_id": 1,
                            "timeout": 2.0,
                            "reconnect_max_retries": 20,
                            "reconnect_backoff_max": 0.5,
                            # mock server 寄存器布局为 big-endian float32
                            "word_order": "big_endian",
                        },
                    },
                    "device_group": "wtg",
                    "enabled": True,
                },
                {
                    "device_id": "iec104-1",
                    "protocol": "iec104",
                    "point_table": "iec104",
                    "endpoint": {
                        "host": "127.0.0.1",
                        "port": iec104_port,
                        "extensions": {"common_addr": 1, "t1": 2.0, "t2": 1.0, "t3": 5.0},
                    },
                    "device_group": "wtg",
                    "enabled": True,
                },
            ]
        },
    )
    _write_yaml(
        config_dir,
        "points.yaml",
        {
            "point_tables": {
                "modbus": {
                    "points": [
                        {
                            "point_id": "rotor.speed",
                            "point_groups": ["telemetry"],
                            "address": {"register_type": "holding", "address": 100},
                            "data_type": "float32",
                            "scale": _MODBUS_SCALE,
                            "offset": _MODBUS_OFFSET,
                        },
                        {
                            "point_id": "gen.power",
                            "point_groups": ["telemetry"],
                            "address": {"register_type": "holding", "address": 102},
                            "data_type": "float32",
                        },
                        {
                            "point_id": "setpoint.power",
                            "point_groups": ["telemetry"],
                            "address": {"register_type": "holding", "address": 200},
                            "data_type": "float32",
                        },
                    ]
                },
                "iec104": {
                    "points": [
                        {
                            "point_id": "rotor.speed",
                            "point_groups": ["telemetry"],
                            "address": {"ioa": 100},
                            "data_type": "float32",
                            "scale": _IEC104_SCALE,
                        },
                        {
                            "point_id": "gen.power",
                            "point_groups": ["telemetry"],
                            "address": {"ioa": 200},
                            "data_type": "float32",
                        },
                        {
                            "point_id": "power.setpoint",
                            "point_groups": ["commands"],
                            "address": {"ioa": 300},
                            "data_type": "float32",
                        },
                    ]
                },
            }
        },
    )
    _write_yaml(
        config_dir,
        "tasks.yaml",
        {
            "tasks": [
                {
                    "task_id": "modbus-telemetry",
                    "device": "modbus-1",
                    "point_group": "telemetry",
                    "interval": 0.2,
                    "targets": [{"sink": "file_sink"}],
                },
                {
                    "task_id": "iec104-telemetry",
                    "device": "iec104-1",
                    "point_group": "telemetry",
                    "interval": 0.5,
                    "targets": [{"sink": "file_sink"}],
                },
                {
                    "task_id": "group-telemetry",
                    "device_group": "wtg",
                    "point_group": "telemetry",
                    "interval": 0.3,
                    "targets": [{"sink": "file_sink"}],
                },
            ]
        },
    )


@pytest.fixture
async def modbus_server():
    server = ModbusMockServer(port=_free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def iec104_server():
    # 远控版 server（port=0 动态端口）：支持总召 + C_SE_NC_1 等控制命令，
    # 数据只在总召后上送——收到数据即证明 STARTDT/总召链路真实打通。
    server = IEC104ControlServer(common_addr=1, data_points={100: _RAW_IEC104_ROTOR, 200: 50.0})
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def system(
    tmp_path: Path, modbus_server: ModbusMockServer, iec104_server: IEC104ControlServer
):
    """完整装配并启动的系统；测试结束优雅停机（Task 实例默认 STOPPED）。"""
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    sink_path = tmp_path / "out" / "data.jsonl"
    _build_config(config_dir, modbus_server.port, iec104_server.port, sink_path)

    rt = assemble(config_dir)
    await start_runtime(rt)
    try:
        yield rt, config_dir, sink_path
    finally:
        from wind_hub.assembly import stop_runtime

        await stop_runtime(rt)


async def _wait_sink_point(
    rt: AssembledRuntime,
    sink_path: Path,
    device_id: str,
    point_id: str,
    timeout: float = 5.0,
) -> dict:
    """flush FileSink 后轮询落盘文件，返回目标点的最新一行（jsonl dict）。"""
    sink = rt.sinks["file_sink"]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await sink.flush()
        if sink_path.exists():
            for line in reversed(sink_path.read_text(encoding="utf-8").splitlines()):
                row = json.loads(line)
                if row["device_id"] == device_id and row["point_id"] == point_id:
                    return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"file sink 在 {timeout}s 内未收到 {device_id}/{point_id}")


# ---------------------------------------------------------------------------
# 1. 单一启动快照
# ---------------------------------------------------------------------------


async def test_single_startup_snapshot(system) -> None:
    """ConfigUseCase.current_config 与装配启动快照同源（同一次 load_config）。"""
    rt, _config_dir, _sink_path = system

    assert rt.config.current_config is rt.boot_config
    # 启动快照内容与 Runtime 实际组件一致
    assert {d.device_id for d in rt.boot_config.devices.devices} == set(rt.runtime.devices)
    assert {s.name for s in rt.boot_config.system.sinks} == set(rt.sinks)
    assert {t.task_id for t in rt.boot_config.tasks.tasks} == set(rt.runtime.task_definitions())


# ---------------------------------------------------------------------------
# 2. Task Definition → Instance → 生命周期（公共入口）
# ---------------------------------------------------------------------------


async def test_instances_default_stopped_and_group_expansion(system) -> None:
    """实例默认 STOPPED；device_group Task 展开为每设备一个实例。"""
    rt, _config_dir, _sink_path = system

    instances = {i.instance_id: i for i in await rt.tasks.list_instances()}
    assert set(instances) == {
        _MODBUS_IID,
        _IEC104_IID,
        "group-telemetry:modbus-1",
        "group-telemetry:iec104-1",
    }
    assert all(i.state.value == "stopped" for i in instances.values())


async def test_instance_start_stop_lifecycle(system) -> None:
    """TaskUseCase 公共入口：STOPPED → RUNNING（产出数据）→ STOPPED（停产）。"""
    rt, _config_dir, sink_path = system

    detail = await rt.tasks.start_instance(_MODBUS_IID)
    assert detail.state.value == "running"

    await _wait_sink_point(rt, sink_path, "modbus-1", "rotor.speed")

    detail = await rt.tasks.stop_instance(_MODBUS_IID)
    assert detail.state.value == "stopped"

    sink = rt.sinks["file_sink"]
    await sink.flush()
    marker = len(sink_path.read_text(encoding="utf-8").splitlines())
    await asyncio.sleep(0.5)  # interval 0.2s 的 2.5 倍——运行中必然有新行
    await sink.flush()
    assert len(sink_path.read_text(encoding="utf-8").splitlines()) == marker


# ---------------------------------------------------------------------------
# 3. Modbus 真实链路（含 scale/offset）
# ---------------------------------------------------------------------------


async def test_modbus_poll_chain_with_scaling(system) -> None:
    """真实 socket 采集：寄存器原始值 1200.5 → 工程值 1200.5*2+10 落盘。"""
    rt, _config_dir, sink_path = system
    await rt.tasks.start_instance(_MODBUS_IID)

    row = await _wait_sink_point(rt, sink_path, "modbus-1", "rotor.speed")
    assert row["value"] == pytest.approx(_RAW_ROTOR_SPEED * _MODBUS_SCALE + _MODBUS_OFFSET)
    assert row["quality"] == Quality.GOOD.value
    assert row["source"] == "modbus"
    assert row["timestamp"]

    gen = await _wait_sink_point(rt, sink_path, "modbus-1", "gen.power")
    assert gen["value"] == pytest.approx(_RAW_GEN_POWER)


# ---------------------------------------------------------------------------
# 4. IEC104 SUBSCRIBE + 总召链路
# ---------------------------------------------------------------------------


async def test_iec104_subscribe_interrogation_chain(system) -> None:
    """SUBSCRIBE 启动后自动总召——Server 只在总召后上送数据，收到即证明链路。

    IOA 100 → rotor.speed，工程值 1500.5 * 0.5；quality / timestamp 如实保留。
    """
    rt, _config_dir, sink_path = system
    await rt.tasks.start_instance(_IEC104_IID)

    row = await _wait_sink_point(rt, sink_path, "iec104-1", "rotor.speed")
    assert row["value"] == pytest.approx(_RAW_IEC104_ROTOR * _IEC104_SCALE)
    assert row["quality"] == Quality.GOOD.value
    assert row["timestamp"]


# ---------------------------------------------------------------------------
# 5. QueryUseCase
# ---------------------------------------------------------------------------


async def test_query_usecase_against_real_servers(system) -> None:
    """list/get/read_point/status 全部穿透到真实 Server（read_point 非缓存）。"""
    rt, _config_dir, sink_path = system
    await rt.tasks.start_instance(_MODBUS_IID)
    await _wait_sink_point(rt, sink_path, "modbus-1", "rotor.speed")

    devices = await rt.query.list_devices()
    assert {d.device_id for d in devices} == {"modbus-1", "iec104-1"}
    assert all(d.connected for d in devices)

    info = await rt.query.get_device_info("modbus-1")
    assert info.protocol == "modbus"
    assert info.connected is True

    # read_point 实时穿透到 Modbus Server，且工程值换算与采集路径一致
    pv = await rt.query.read_point("modbus-1", "rotor.speed")
    assert pv.value == pytest.approx(_RAW_ROTOR_SPEED * _MODBUS_SCALE + _MODBUS_OFFSET)

    status = await rt.query.status()
    assert status.running is True
    assert status.device_count == 2
    assert status.sink_count == 1
    assert status.devices_connected == 2
    assert status.points_collected > 0
    assert status.points_routed >= status.points_collected
    assert status.points_dropped == 0
    # acquisitions 覆盖全部实例（采集执行状态与实例启停分维度，经
    # TaskUseCase.get_instance 核实启停状态）
    assert {a.instance_id for a in status.acquisitions} == {
        _MODBUS_IID,
        _IEC104_IID,
        "group-telemetry:modbus-1",
        "group-telemetry:iec104-1",
    }
    assert (await rt.tasks.get_instance(_MODBUS_IID)).state.value == "running"
    assert (await rt.tasks.get_instance(_IEC104_IID)).state.value == "stopped"


# ---------------------------------------------------------------------------
# 6. CommandUseCase —— Server 侧确认
# ---------------------------------------------------------------------------


async def test_modbus_command_verified_on_server(system, modbus_server: ModbusMockServer) -> None:
    """CommandUseCase → Device.write → Modbus Server：从站寄存器真的被改写。"""
    rt, _config_dir, _sink_path = system

    result = await rt.command.send(
        Command(
            command_id="fs-modbus-1", device_id="modbus-1", point_id="setpoint.power", value=66.5
        )
    )
    assert result.success is True

    # 服务器侧确认（float32 双寄存器 big-endian）
    regs = await modbus_server.read_holding(1, 200, 2)
    assert struct.unpack(">f", struct.pack(">HH", *regs))[0] == pytest.approx(66.5)

    # 实时读回一致
    pv = await rt.query.read_point("modbus-1", "setpoint.power")
    assert pv.value == pytest.approx(66.5)


async def test_iec104_command_verified_on_server(
    system, iec104_server: IEC104ControlServer
) -> None:
    """IEC104 set-point 命令（C_SE_NC_1）到达 Server 并被确认。"""
    rt, _config_dir, _sink_path = system

    result = await rt.command.send(
        Command(
            command_id="fs-iec104-1", device_id="iec104-1", point_id="power.setpoint", value=42.5
        )
    )
    assert result.success is True

    # C_SE_NC_1 = TypeID 50；server 记录最后一次控制的 IOA
    deadline = asyncio.get_event_loop().time() + 2.0
    while asyncio.get_event_loop().time() < deadline:
        if iec104_server.last_control.get("50") == 300:
            break
        await asyncio.sleep(0.05)
    assert iec104_server.last_control.get("50") == 300


# ---------------------------------------------------------------------------
# 7. Web API 主要入口（ASGI 全链路，底层协议仍连真实 Server）
# ---------------------------------------------------------------------------


async def test_webapi_main_endpoints(system) -> None:
    rt, _config_dir, sink_path = system
    set_runtime_context(rt)
    from wind_hub.adapter.inbound.webapi.app import build_api

    transport = ASGITransport(app=build_api())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # /health 即系统状态快照（无独立 /status 路由）
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["running"] is True
            assert health.json()["status"] == "ok"
            assert health.json()["device_count"] == 2
            assert health.json()["devices_connected"] == 2

            metrics = await client.get("/metrics")
            assert metrics.status_code == 200

            devices = await client.get("/devices")
            assert {d["device_id"] for d in devices.json()} == {"modbus-1", "iec104-1"}

            tasks = await client.get("/tasks")
            assert {t["task_id"] for t in tasks.json()} == {
                "modbus-telemetry",
                "iec104-telemetry",
                "group-telemetry",
            }

            instances = await client.get("/tasks/instances")
            assert len(instances.json()) == 4

            started = await client.post(f"/tasks/instances/{_MODBUS_IID}/start")
            assert started.status_code == 200
            assert started.json()["state"] == "running"
            await _wait_sink_point(rt, sink_path, "modbus-1", "rotor.speed")

            point = await client.get("/points/modbus-1/rotor.speed")
            assert point.status_code == 200
            assert point.json()["value"] == pytest.approx(
                _RAW_ROTOR_SPEED * _MODBUS_SCALE + _MODBUS_OFFSET
            )

            cmd = await client.post(
                "/commands",
                json={"device_id": "modbus-1", "point_id": "setpoint.power", "value": 33.0},
            )
            assert cmd.status_code == 200
            assert cmd.json()["success"] is True

            stopped = await client.post(f"/tasks/instances/{_MODBUS_IID}/stop")
            assert stopped.status_code == 200
            assert stopped.json()["state"] == "stopped"

            reload_resp = await client.post("/config/reload")
            assert reload_resp.status_code == 200
            assert reload_resp.json()["success"] is True
    finally:
        clear_runtime_context()


# ---------------------------------------------------------------------------
# 8. 热重载：Task 字段变化 + 点表轻量变化
# ---------------------------------------------------------------------------


async def test_reload_task_change_and_point_table_scale(system) -> None:
    """reload 走「磁盘新配置 → diff → reconfigure → 快照提交」完整路径。

    - Task 变化：interval 0.2 → 0.1（tasks.updated）；
    - 轻量变化：点表 scale 2.0 → 3.0（points_changed，Device.set_points
      生效——后续落盘值变为 raw*3+10）。
    """
    rt, config_dir, sink_path = system
    await rt.tasks.start_instance(_MODBUS_IID)
    await _wait_sink_point(rt, sink_path, "modbus-1", "rotor.speed")

    # Task 字段变化
    tasks = yaml.safe_load((config_dir / "tasks.yaml").read_text(encoding="utf-8"))
    tasks["tasks"][0]["interval"] = 0.1
    _write_yaml(config_dir, "tasks.yaml", tasks)

    result = await rt.config.reload()
    assert result.success is True
    assert result.diff.tasks.updated == ["modbus-telemetry"]
    assert rt.config.current_config is not rt.boot_config
    assert rt.config.current_config.tasks.tasks[0].interval == 0.1

    # 点表轻量变化（scale）——不重建连接，set_points 新值立即生效
    points = yaml.safe_load((config_dir / "points.yaml").read_text(encoding="utf-8"))
    points["point_tables"]["modbus"]["points"][0]["scale"] = 3.0
    _write_yaml(config_dir, "points.yaml", points)

    result = await rt.config.reload()
    assert result.success is True
    assert result.diff.points_changed is True

    row = await _wait_sink_point_after(rt, sink_path, "modbus-1", "rotor.speed")
    assert row["value"] == pytest.approx(_RAW_ROTOR_SPEED * 3.0 + _MODBUS_OFFSET)


async def _wait_sink_point_after(
    rt: AssembledRuntime,
    sink_path: Path,
    device_id: str,
    point_id: str,
    timeout: float = 5.0,
) -> dict:
    """等待一条**比当前文件行数更新**的目标点记录（reload 后新批次）。"""
    sink = rt.sinks["file_sink"]
    await sink.flush()
    baseline = len(sink_path.read_text(encoding="utf-8").splitlines())
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await sink.flush()
        lines = sink_path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines[baseline:]):
            row = json.loads(line)
            if row["device_id"] == device_id and row["point_id"] == point_id:
                return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"reload 后 {timeout}s 内未收到新的 {device_id}/{point_id}")
