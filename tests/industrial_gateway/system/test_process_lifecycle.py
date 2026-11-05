"""真实子进程生命周期：--check、启动服务、SIGTERM 干净退出、SIGKILL 后端口重绑。

启动生产入口 ``python -m industrial_gateway.main``，不替换任何生产对象；
Commander 端点指向不可达端口（懒连接，不影响启动与 Modbus 数据面）。
"""

from __future__ import annotations

import asyncio
import signal
import struct
import sys
from pathlib import Path

import pytest

from tests.support.gateway_v1 import (
    ServerSpec,
    reserve_port,
    write_config_tree,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service]


async def _wait_port_open(port: int, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=0.5
            )
            writer.close()
            return
        except (TimeoutError, ConnectionError, OSError):
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)


async def _assert_port_closed(port: int) -> None:
    with pytest.raises((ConnectionError, OSError)):
        await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1.0)


async def _fc03(port: int, unit: int, address: int, count: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        pdu = struct.pack(">BHH", 0x03, address, count)
        writer.write(struct.pack(">HHHB", 700, 0, len(pdu) + 1, unit) + pdu)
        header = await reader.readexactly(7)
        tid, _proto, length, _unit = struct.unpack(">HHHB", header)
        assert tid == 700
        return await reader.readexactly(length - 1)
    finally:
        writer.close()


def _write_tree(root: Path) -> tuple[Path, int, int]:
    modbus_port = reserve_port()
    grpc_port = reserve_port()
    commander_port = reserve_port()  # 无监听：转发懒连接
    specs = [
        ServerSpec(
            server_id="gw1",
            device_id="D01",
            modbus_port=modbus_port,
            control_enabled=False,
            command_mappings=(),
        )
    ]
    service_path = write_config_tree(
        root,
        specs,
        commander_endpoint=f"127.0.0.1:{commander_port}",
        grpc_port=grpc_port,
    )
    return service_path, modbus_port, grpc_port


async def _spawn(service_path: Path, root: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "industrial_gateway.main",
        "--config",
        str(service_path),
        "--project-root",
        str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def test_cli_check_valid_config(tmp_path: Path) -> None:
    root = tmp_path / "root"
    service_path, _, _ = _write_tree(root)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "industrial_gateway.main",
        "--config",
        str(service_path),
        "--project-root",
        str(root),
        "--check",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, stderr.decode()
    assert b"config valid" in stderr


async def test_factory_shipped_config_passes_native_check() -> None:
    """仓库出厂配置 configs/services/industrial_gateway.yaml 必须被生产入口接受。"""
    repo_root = Path(__file__).resolve().parents[3]
    service = repo_root / "configs" / "services" / "industrial_gateway.yaml"
    assert service.is_file()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "industrial_gateway.main",
        "--config",
        str(service),
        "--project-root",
        str(repo_root),
        "--check",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, stderr.decode()
    assert b"config valid" in stderr


async def test_cli_check_missing_config_fails(tmp_path: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "industrial_gateway.main",
        "--config",
        str(tmp_path / "absent.yaml"),
        "--project-root",
        str(tmp_path),
        "--check",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    assert proc.returncode != 0


async def test_start_serve_sigterm_and_port_release(tmp_path: Path) -> None:
    root = tmp_path / "root"
    service_path, modbus_port, grpc_port = _write_tree(root)
    proc = await _spawn(service_path, root)
    try:
        await _wait_port_open(modbus_port)
        await _wait_port_open(grpc_port)
        body = await _fc03(modbus_port, 1, 110, 1)  # fb_uint 未初始化
        assert body[0] == 0x03 and body[1] == 2 and body[2:4] == b"\x00\x00"
        proc.send_signal(signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=8.0)
        assert proc.returncode == 0
        await _assert_port_closed(modbus_port)
        await _assert_port_closed(grpc_port)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_restart_after_sigkill_rebinds_ports(tmp_path: Path) -> None:
    """进程异常退出（崩溃/SIGKILL）后同配置立即重启必须成功重绑并恢复服务。"""
    root = tmp_path / "root"
    service_path, modbus_port, grpc_port = _write_tree(root)
    first = await _spawn(service_path, root)
    try:
        await _wait_port_open(modbus_port)
        first.kill()
        await asyncio.wait_for(first.wait(), timeout=5.0)
    finally:
        if first.returncode is None:
            first.kill()
            await first.wait()

    second = await _spawn(service_path, root)
    try:
        await _wait_port_open(modbus_port)
        await _wait_port_open(grpc_port)
        body = await _fc03(modbus_port, 1, 110, 1)
        assert body[0] == 0x03
        second.send_signal(signal.SIGTERM)
        await asyncio.wait_for(second.wait(), timeout=8.0)
        assert second.returncode == 0
    finally:
        if second.returncode is None:
            second.kill()
            await second.wait()
