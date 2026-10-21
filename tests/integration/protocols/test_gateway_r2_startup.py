"""D1 回归：出厂配置真实进程 --check / 启动监听 / 正常停止。

直接以子进程方式运行 ``python -m industrial_gateway.main``，不注入任何模拟对象；
绑定出厂配置中的固定端口 15020。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pymodbus.client import AsyncModbusTcpClient

REPO_ROOT = Path(__file__).resolve().parents[3]
FACTORY_CONFIG = REPO_ROOT / "configs" / "industrial_gateway.yaml"
LISTEN_PORT = 15020

pytestmark = [pytest.mark.modbus, pytest.mark.real_service]


def _port_in_use() -> bool:
    """True 仅当仍有 LISTEN；TIME_WAIT 不视为占用（SO_REUSEADDR 可绑定）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", LISTEN_PORT))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "industrial_gateway.main", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_factory_config_check() -> None:
    result = _run_module("--config", str(FACTORY_CONFIG), "--check")
    assert result.returncode == 0, result.stderr
    assert "configuration valid" in result.stdout + result.stderr


async def test_factory_config_starts_serves_and_stops() -> None:
    if _port_in_use():
        pytest.fail(f"factory port {LISTEN_PORT} already in use before startup")

    env = dict(os.environ)
    src_path = str(REPO_ROOT / "src")
    if not _imports_from_installed_package():
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.Popen(
        [sys.executable, "-m", "industrial_gateway.main", "--config", str(FACTORY_CONFIG)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 15
        listening = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"gateway exited early ({proc.returncode}):\n{output}")
            try:
                with socket.create_connection(("127.0.0.1", LISTEN_PORT), timeout=0.5):
                    listening = True
                    break
            except OSError:
                time.sleep(0.2)
        assert listening, "gateway did not open the factory listen port"

        # 真实 pymodbus 客户端必须完成一次完整 Modbus 事务（空存储返回设备故障异常
        # 也是合法 Modbus 响应，证明协议栈完整工作）。
        client = AsyncModbusTcpClient("127.0.0.1", port=LISTEN_PORT, timeout=5)
        await client.connect()
        try:
            assert client.connected
            response = await client.read_holding_registers(address=100, count=2, device_id=1)
            assert response.function_code in (3, 131)
        finally:
            client.close()

        proc.send_signal(signal.SIGTERM)
        exit_deadline = time.monotonic() + 10
        while time.monotonic() < exit_deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() == 0, f"gateway did not stop cleanly: rc={proc.poll()}"

        for _ in range(50):
            if _port_in_use():
                time.sleep(0.1)
            else:
                break
        assert not _port_in_use(), "listen port not released after shutdown"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _imports_from_installed_package() -> bool:
    """editable 安装指向本仓库 src 时无需额外 PYTHONPATH。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import industrial_gateway, sys; print(industrial_gateway.__file__)",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    path = result.stdout.strip()
    return bool(path) and REPO_ROOT.resolve() in Path(path).resolve().parents
