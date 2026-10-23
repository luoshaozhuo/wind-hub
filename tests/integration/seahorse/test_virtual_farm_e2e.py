"""50 台虚拟风机真实进程、Modbus TCP 和双向 gRPC 集成验收。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from pymodbus.client import ModbusTcpClient

from industrial_gateway.infrastructure.config.loader import load_config
from industrial_gateway.infrastructure.protocol.modbus.codec import (
    RegisterMapping,
    decode_registers,
)
from seahorse.infrastructure.dc_ems_generator import generate_profile

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "configs" / "seahorse_dc_ems_manifest.json"


@contextmanager
def _process(module: str, *arguments: str) -> Iterator[subprocess.Popen[str]]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, "-m", module, *arguments],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def _ready(process: subprocess.Popen[str], port: int, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"process exited early: {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"port {port} did not become ready")


def _read(client: ModbusTcpClient, point: RegisterMapping) -> int | float | bool:
    if point.area == "holding":
        response = client.read_holding_registers(point.address, count=point.count, device_id=1)
    elif point.area == "input":
        response = client.read_input_registers(point.address, count=point.count, device_id=1)
    elif point.area == "coil":
        response = client.read_coils(point.address, count=1, device_id=1)
    else:
        response = client.read_discrete_inputs(point.address, count=1, device_id=1)
    assert not response.isError(), response
    if point.area in {"coil", "discrete_input"}:
        return bool(response.bits[0])
    return decode_registers(point, response.registers)


@pytest.mark.integration
@pytest.mark.modbus
@pytest.mark.real_service
@pytest.mark.network
def test_50_turbine_real_process_bidirectional(tmp_path: Path) -> None:
    """全部设备真实遥测，至少一台执行真实控制并回读独立状态。"""
    generate_profile(MANIFEST, tmp_path, enable_control=True)
    gateway_path = tmp_path / "gateway.yaml"
    seahorse_path = tmp_path / "seahorse.yaml"
    gateway = load_config(gateway_path)
    assert len(gateway.devices) == len(gateway.endpoints) == 50
    assert gateway.data_grpc_bind == "127.0.0.1:50073"
    table = gateway.point_tables["dc_ems_turbine"].points

    def mapping(device_id: str, name: str) -> RegisterMapping:
        point = table[name]
        return RegisterMapping(
            device_id,
            name,
            point.address.register_type,
            point.address.address,
            point.data_type,
            point.address.word_order,
        )

    with _process("industrial_gateway.main", "--config", str(gateway_path)) as gateway_proc:
        _ready(gateway_proc, 50073)
        for endpoint in gateway.endpoints.values():
            _ready(gateway_proc, endpoint.port)
        with _process(
            "seahorse.main",
            "--config",
            str(seahorse_path),
            "--gateway-config",
            str(gateway_path),
            "--gateway-grpc-endpoint",
            "127.0.0.1:50073",
            "--command-bind",
            "127.0.0.1:50072",
        ) as seahorse:
            _ready(seahorse, 50072)
            for device_id, device in gateway.devices.items():
                endpoint = gateway.endpoints[device.endpoint_id]
                with ModbusTcpClient(endpoint.host, port=endpoint.port, timeout=2) as client:
                    assert client.connected
                    deadline = time.monotonic() + 8
                    while True:
                        assert seahorse.poll() is None
                        wind = _read(client, mapping(device_id, "emsWinSpd"))
                        if isinstance(wind, float) and wind > 0:
                            break
                        if time.monotonic() > deadline:
                            raise AssertionError(f"no live telemetry from {device_id}")
                        time.sleep(0.1)

            first_id = next(iter(gateway.devices))
            endpoint = gateway.endpoints[gateway.devices[first_id].endpoint_id]
            with ModbusTcpClient(endpoint.host, port=endpoint.port, timeout=2) as client:
                assert client.connected
                assert _read(client, mapping(first_id, "emsWorkMode")) == 1
                stop = mapping(first_id, "ctrlStop")
                response = client.write_coil(stop.address, True, device_id=1)
                assert not response.isError(), response
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if _read(client, mapping(first_id, "emsWorkMode")) == 0:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("stop command did not reach turbine state")
                assert seahorse.poll() is None
                assert gateway_proc.poll() is None
