"""probe 对本测试真实协议 Server 的集成验证。

既有 ``tests/integration/adapter/inbound/probe/`` 的 e2e 大多在协议层
mock 驱动；本文件补齐**真实链路**：probe 的函数级 API 与 2 条 CLI 主链
直接打在本测试启动的真实 Modbus TCP Server 上（127.0.0.1 + 动态端口，
扫描范围仅限回环与本测试端口，不做无界局域网扫描）。

CLI 用例是同步测试（typer 命令内部 ``asyncio.run``，不能在已运行的
事件循环里调），因此 Modbus Server 跑在**专用线程的独立事件循环**中。
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tests.fixtures.servers.modbus_server import ModbusMockServer
from wind_hub.adapter.inbound.cli.app import build_cli
from wind_hub.adapter.inbound.cli.probe.diagnose import diagnose_device
from wind_hub.adapter.inbound.cli.probe.diagnose_models import StepStatus
from wind_hub.adapter.inbound.cli.probe.discover import discover_modbus
from wind_hub.adapter.inbound.cli.probe.local_info import get_local_info
from wind_hub.adapter.inbound.cli.probe.ports import scan_ports
from wind_hub.adapter.inbound.cli.probe.ports_models import PortState
from wind_hub.adapter.inbound.cli.probe.scan_methods import scan_tcp
from wind_hub.adapter.inbound.cli.probe.verify import verify_device
from wind_hub.config.schema import DeviceConfig, Endpoint, PointAddress, PointConfig


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _modbus_device_config(port: int, device_id: str = "probe-modbus") -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id,
        protocol="modbus",
        point_table="probe-table",
        endpoint=Endpoint(
            host="127.0.0.1",
            port=port,
            extensions={"unit_id": 1, "timeout": 2.0},
        ),
    )


def _modbus_points() -> list[PointConfig]:
    return [
        PointConfig(
            point_id="rotor.speed",
            point_groups=["telemetry"],
            address=PointAddress(register_type="holding", address=100),
            data_type="float32",
        ),
        PointConfig(
            point_id="gen.power",
            point_groups=["telemetry"],
            address=PointAddress(register_type="holding", address=102),
            data_type="float32",
        ),
    ]


@pytest.fixture
async def modbus_server():
    server = ModbusMockServer(port=_free_port())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# 端口扫描（真实 socket，仅回环 + 本测试端口）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_ports_open_and_closed(modbus_server: ModbusMockServer) -> None:
    """本测试 Server 端口 OPEN；刚释放的空闲端口 CLOSED。"""
    closed_port = _free_port()
    result = await scan_ports("127.0.0.1", [modbus_server.port, closed_port], timeout=1.0)

    by_port = {p.port: p for p in result.ports}
    assert by_port[modbus_server.port].state is PortState.OPEN
    assert by_port[closed_port].state is PortState.CLOSED
    assert result.total == 2
    assert result.open_count == 1


@pytest.mark.asyncio
async def test_scan_tcp_finds_test_port(modbus_server: ModbusMockServer) -> None:
    """scan_tcp 用指定端口列表探测——只扫 127.0.0.1 与本测试端口。"""
    alive = await scan_tcp(["127.0.0.1"], [modbus_server.port], timeout=1.0, concurrency=8)
    assert alive == {"127.0.0.1"}

    dead = await scan_tcp(["127.0.0.1"], [_free_port()], timeout=0.3, concurrency=8)
    assert dead == set()


# ---------------------------------------------------------------------------
# discover / verify / diagnose —— 真实协议驱动打真实 Server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_modbus_real_small_range(modbus_server: ModbusMockServer) -> None:
    """小范围保持寄存器扫描命中本测试 Server 的寄存器布局。"""
    points = await discover_modbus(_modbus_device_config(modbus_server.port), [(100, 104)])

    symbols = {p.symbol for p in points}
    # 布局内 100-104 全部可读（连续数据块），扫描结果标注「需人工确认」
    assert symbols == {f"holding[{a}]" for a in range(100, 105)}
    assert all(p.comment == "扫描结果，需人工确认" for p in points)


@pytest.mark.asyncio
async def test_verify_device_real_success(modbus_server: ModbusMockServer) -> None:
    """verify_device 真实建驱动/connect/read：点全部 ok 且值与 Server 一致。"""
    result = await verify_device(
        _modbus_device_config(modbus_server.port), _modbus_points(), connect_timeout=3.0
    )

    assert result.connect_ok is True
    assert result.connect_error is None
    by_id = {p.point_id: p for p in result.points}
    assert by_id["rotor.speed"].ok is True
    assert by_id["rotor.speed"].value == pytest.approx(1200.5)
    assert by_id["rotor.speed"].quality == "good"
    assert by_id["gen.power"].ok is True
    assert by_id["gen.power"].value == pytest.approx(800.0)


@pytest.mark.asyncio
async def test_verify_device_real_failure_wrong_port() -> None:
    """端口无监听：connect 失败 → 所有点 fail，connect_error 有说明。"""
    result = await verify_device(
        _modbus_device_config(_free_port()), _modbus_points(), connect_timeout=2.0
    )

    assert result.connect_ok is False
    assert result.connect_error
    assert result.points
    assert all(not p.ok for p in result.points)


@pytest.mark.asyncio
async def test_diagnose_device_real_driver_healthy(modbus_server: ModbusMockServer) -> None:
    """diagnose 全分层真实（ARP/ICMP/TCP/真实协议驱动 connect+read）→ 健康。"""
    result = await diagnose_device(
        _modbus_device_config(modbus_server.port), _modbus_points(), connect_timeout=3.0
    )

    assert result.device_ip == "127.0.0.1"
    assert result.same_subnet is True  # 回环视为同网段
    assert result.overall is StepStatus.OK
    # 协议层未跳过且成功（真实 Modbus 握手 + 读点）
    protocol_steps = [s for s in result.steps if not s.skipped]
    assert any("MODBUS" in s.name for s in protocol_steps)


@pytest.mark.asyncio
async def test_diagnose_device_real_driver_server_down(
    modbus_server: ModbusMockServer,
) -> None:
    """Server 停止后再诊断：TCP 层失败，协议层跳过，overall FAIL。"""
    port = modbus_server.port
    await modbus_server.stop()

    result = await diagnose_device(_modbus_device_config(port), connect_timeout=1.0)

    assert result.overall is StepStatus.FAIL
    tcp_steps = [s for s in result.steps if s.name.startswith("TCP")]
    assert tcp_steps and tcp_steps[0].status is StepStatus.FAIL
    protocol_step = [s for s in result.steps if "MODBUS" in s.name]
    assert protocol_step and protocol_step[0].skipped is True


@pytest.mark.asyncio
async def test_get_local_info_valid() -> None:
    """本机信息字段有效（回环已过滤，至少保留 hostname）。"""
    info = await get_local_info()

    assert info.hostname
    for cidr, iface in info.ips:
        assert "/" in cidr
        assert iface
        assert not cidr.startswith("127.")


# ---------------------------------------------------------------------------
# CLI 主链（真实 Modbus Server，专用线程跑 Server 事件循环）
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _modbus_server_in_thread(port: int) -> Iterator[ModbusMockServer]:
    """在专用线程的独立事件循环里跑 ModbusMockServer。

    typer 命令内部 ``asyncio.run`` 要求同步测试上下文；pymodbus server
    是 async 的，必须有自己的 loop。
    """
    loop = asyncio.new_event_loop()
    server = ModbusMockServer(port=port)
    ready = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.start())
        except BaseException as exc:  # noqa: BLE001 — 透传给主线程断言
            errors.append(exc)
            ready.set()
            return
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    if not ready.wait(timeout=5.0):
        raise RuntimeError("ModbusMockServer 线程启动超时")
    if errors:
        raise errors[0]
    try:
        yield server
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
        loop.close()


def _write_cli_config(base: Path, port: int) -> None:
    """probe verify CLI 的最小配置目录（设备指向本测试 Server）。"""
    (base / "system.yaml").write_text("{}\n", encoding="utf-8")
    (base / "devices.yaml").write_text(
        yaml.safe_dump(
            {
                "devices": [
                    {
                        "device_id": "probe-modbus",
                        "protocol": "modbus",
                        "point_table": "probe-table",
                        "endpoint": {
                            "host": "127.0.0.1",
                            "port": port,
                            "extensions": {"unit_id": 1, "timeout": 2.0},
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (base / "points.yaml").write_text(
        yaml.safe_dump(
            {
                "point_tables": {
                    "probe-table": {
                        "points": [
                            {
                                "point_id": "rotor.speed",
                                "point_groups": ["telemetry"],
                                "address": {"register_type": "holding", "address": 100},
                                "data_type": "float32",
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (base / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")


def test_cli_probe_verify_real_modbus(tmp_path: Path) -> None:
    """CLI 主链 1：``probe verify`` 真实驱动读真实 Server → exit 0 全 ok。"""
    port = _free_port()
    with _modbus_server_in_thread(port):
        _write_cli_config(tmp_path, port)
        result = CliRunner().invoke(build_cli(), ["probe", "verify", "--config", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "设备: probe-modbus (modbus)" in result.output


def test_cli_probe_ports_real_server() -> None:
    """CLI 主链 2：``probe ports`` 扫到本测试 Server 端口 OPEN。"""
    port = _free_port()
    with _modbus_server_in_thread(port):
        result = CliRunner().invoke(
            build_cli(),
            ["probe", "ports", "--host", "127.0.0.1", "--ports", str(port)],
        )

    assert result.exit_code == 0, result.output
    assert str(port) in result.output
    assert "open" in result.output.lower()


# 其余 CLI 主链（scan / diagnose / discover、json/yaml 输出、exit code
# 语义）由 tests/integration/adapter/inbound/probe/ 的既有 e2e 与
# tests/unit/adapter/inbound/probe/ 的单测覆盖，此处不重复。
