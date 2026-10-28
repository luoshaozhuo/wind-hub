"""50 台虚拟风机真实进程、Modbus TCP 和双向 gRPC 集成验收。"""

from __future__ import annotations

import json
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
    encode_registers,
)
from seahorse.infrastructure.dc_ems_generator import generate_profile

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
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
            _, stderr = process.communicate()
            raise AssertionError(
                f"process exited early: {process.returncode}; stderr: {stderr[-4000:]}"
            )
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
                previous_limit = _read(client, mapping(first_id, "emsActPowerMaxSetFeedback"))
                invalid_limit = mapping(first_id, "ctrlActPow")
                words = encode_registers(invalid_limit, 9000.0)
                rejected = client.write_registers(invalid_limit.address, list(words), device_id=1)
                assert rejected.isError(), rejected
                assert (
                    _read(client, mapping(first_id, "emsActPowerMaxSetFeedback")) == previous_limit
                )
                assert seahorse.poll() is None
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

            # 每台风机具有独立命令路由及模型状态；首台停机不得影响第二台。
            second_id = next(device_id for device_id in gateway.devices if device_id != first_id)
            second_endpoint = gateway.endpoints[gateway.devices[second_id].endpoint_id]
            with ModbusTcpClient(
                second_endpoint.host, port=second_endpoint.port, timeout=2
            ) as second_client:
                assert second_client.connected
                assert _read(second_client, mapping(second_id, "emsWorkMode")) == 1
                response = second_client.write_coil(
                    mapping(second_id, "ctrlStop").address, True, device_id=1
                )
                assert not response.isError(), response
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if _read(second_client, mapping(second_id, "emsWorkMode")) == 0:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("second turbine did not execute its stop command")
            with ModbusTcpClient(endpoint.host, port=endpoint.port, timeout=2) as first_client:
                assert first_client.connected
                assert _read(first_client, mapping(first_id, "emsWorkMode")) == 0
                response = first_client.write_coil(
                    mapping(first_id, "ctrlStart").address, True, device_id=1
                )
                assert not response.isError(), response
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if _read(first_client, mapping(first_id, "emsWorkMode")) == 1:
                        break
                    time.sleep(0.2)
                else:
                    raise AssertionError("first turbine did not restart independently")
            with ModbusTcpClient(
                second_endpoint.host, port=second_endpoint.port, timeout=2
            ) as second_client:
                assert second_client.connected
                assert _read(second_client, mapping(second_id, "emsWorkMode")) == 0


@pytest.mark.integration
@pytest.mark.modbus
@pytest.mark.real_service
@pytest.mark.network
def test_external_device_telemetry_over_real_gateway(tmp_path: Path) -> None:
    """测试专用地址证明非风机模型能真实发布；不代表 DC-EMS 现场地址。"""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["external_devices"] = [
        {
            "id": "substation",
            "model_id": "substation",
            "server_id": 51,
            "ip": "127.0.0.1",
            "original_port": 502,
            "port": 15170,
            "unit_id": 1,
            "points": [
                {
                    "name": "poi_voltage",
                    "role": "feedback",
                    "data_type": "float32",
                    "register_type": "holding",
                    "address": 10,
                    "word_order": "little_endian",
                }
            ],
        },
        {
            "id": "dispatch",
            "model_id": "dispatch",
            "server_id": 52,
            "ip": "127.0.0.1",
            "original_port": 502,
            "port": 15171,
            "unit_id": 1,
            "points": [
                {
                    "name": "emsGridDispatchAct",
                    "role": "feedback",
                    "data_type": "float32",
                    "register_type": "holding",
                    "address": 20,
                    "word_order": "little_endian",
                }
            ],
        },
    ]
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    generate_profile(source, tmp_path)
    gateway_path = tmp_path / "gateway.yaml"
    seahorse_path = tmp_path / "seahorse.yaml"
    gateway = load_config(gateway_path)
    assert len(gateway.devices) == 52

    with _process("industrial_gateway.main", "--config", str(gateway_path)) as gateway_proc:
        _ready(gateway_proc, 50073)
        _ready(gateway_proc, 15170)
        _ready(gateway_proc, 15171)
        with _process(
            "seahorse.main",
            "--config",
            str(seahorse_path),
            "--gateway-config",
            str(gateway_path),
            "--gateway-grpc-endpoint",
            "127.0.0.1:50073",
        ) as seahorse:
            for device_id, name, expected in (
                ("substation", "poi_voltage", 35000.0),
                ("dispatch", "emsGridDispatchAct", 0.0),
            ):
                device = gateway.devices[device_id]
                endpoint = gateway.endpoints[device.endpoint_id]
                point = gateway.point_tables[device.point_table_id].points[name]
                mapping = RegisterMapping(
                    device_id,
                    name,
                    point.address.register_type,
                    point.address.address,
                    point.data_type,
                    point.address.word_order,
                )
                with ModbusTcpClient(endpoint.host, port=endpoint.port, timeout=2) as client:
                    assert client.connected
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        assert seahorse.poll() is None
                        actual = _read(client, mapping)
                        valid = (
                            35000.0 * 0.998 <= actual <= 35000.0 * 1.002
                            if device_id == "substation"
                            else actual == expected
                        )
                        if valid:
                            break
                        time.sleep(0.1)
                    else:
                        raise AssertionError(f"external feedback unavailable: {device_id}.{name}")
            assert gateway_proc.poll() is None
