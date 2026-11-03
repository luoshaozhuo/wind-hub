"""Data Generator 验收测试共享 fixture。

铁律：成功路径一律走真实组件——真实 Core configs 树（site/devices/模板）、
真实 ``assemble_gateway`` 协议栈与正式 ``IndustrialGatewayService``（7 个 RPC，
含 ``UpdateVariables``）、真实 gRPC Server/Client、真实 CSV+SQLite。
故障注入仅用于无法用真实环境稳定构造的超时/挂起，且实现冻结的正式 Proto，
不得替代正式接口。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import grpc
import pytest
import yaml

from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import common_pb2_grpc as common_rpc
from data_generator.application.runtime import Runtime
from data_generator.infrastructure.config import load_settings
from data_generator.infrastructure.gateway import GatewayWriter
from data_generator.infrastructure.server import make_server
from data_generator.infrastructure.snapshots import recover_transaction
from industrial_gateway.assembly import GatewayApplication, assemble_gateway
from industrial_gateway.infrastructure.config.loader import GatewayConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CONFIGS = REPO_ROOT / "configs"
SRC_PATH = REPO_ROOT / "src"


def reserve_port() -> int:
    """预留一个 loopback TCP 端口（测试环境竞态窗口可接受）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# --------------------------------------------------------------------------- #
# 真实 configs 树
# --------------------------------------------------------------------------- #


def _real_tree(root: Path) -> None:
    """从仓库 configs/ 拷贝真实 Core 共享文件（唯一允许的模板来源）。"""
    for name in ("instances", "templates", "history"):
        source = REPO_CONFIGS / name
        target = root / name
        for item in source.rglob("*"):
            if item.is_file():
                destination = target / item.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(item.read_bytes())


def write_service_yaml(
    root: Path,
    *,
    bind: str,
    output_timeout: float = 2.0,
    max_inflight: int = 16,
    diagnose_concurrent: int = 4,
) -> Path:
    """生成服务配置（直接路径契约：相对 configs 根，禁止绝对路径与 ..）。"""
    service = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "configuration": {
            "instances": {
                "site": "instances/site.yaml",
                "devices": "instances/devices.yaml",
                "scenarios": "instances/data-generator.yaml",
            },
            "templates": {"root": "templates"},
        },
        "service": {"grpc_bind": bind},
        "output": {"max_inflight": max_inflight, "grpc_timeout_seconds": output_timeout},
        "buffer": {"max_batches": 128, "max_points_per_batch": 1000},
        "replay": {"read_batch_size": 1000},
        "diagnose": {"max_concurrent": diagnose_concurrent, "timeout_seconds": 10.0},
        "metrics": {
            "sample_seconds": 1.0,
            "window_seconds": 60,
            "retain_seconds": 3600,
            "max_series": 2000,
        },
    }
    path = root / "services" / "data-generator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(service, sort_keys=False), encoding="utf-8")
    return path


def write_scenarios(root: Path, document: dict[str, Any]) -> Path:
    path = root / "instances" / "data-generator.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def write_history(root: Path, name: str, rows: Sequence[str], *, header: bool = True) -> Path:
    path = root / "history" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if header:
        lines.append("device_id,point_id,source_timestamp,value,quality,record_id")
    lines.extend(rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def configs_root(tmp_path: Path) -> Callable[..., Path]:
    """在 tmp_path 下构造受控 configs 根；默认整树拷贝仓库真实 Core 配置。"""

    created: list[Path] = []

    def factory(
        *,
        scenarios: dict[str, Any] | None = None,
        bind: str | None = None,
        output_timeout: float = 2.0,
        max_inflight: int = 16,
        history: dict[str, Sequence[str]] | None = None,
    ) -> Path:
        root = tmp_path / f"configs-{len(created)}"
        created.append(root)
        _real_tree(root)
        if history:
            for name, rows in history.items():
                write_history(root, name, rows)
        if scenarios is not None:
            write_scenarios(root, scenarios)
        service_path = write_service_yaml(
            root,
            bind=bind or f"127.0.0.1:{reserve_port()}",
            output_timeout=output_timeout,
            max_inflight=max_inflight,
        )
        return service_path

    return factory


def ship_scenarios_document(
    gateways: dict[str, str],
    *,
    scenario_id: str = "demo-random",
    interval: float = 1.0,
    seed: int = 42,
    minimum: float = 3.0,
    maximum: float = 15.0,
    device: str = "GW01",
    point: str = "a02_dm_pitchangle1",
    mappings: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """基于真实 Core 点表点 a02_dm_pitchangle1 构造场景文档。"""
    if mappings is None:
        mappings = [(gateway_id, var) for gateway_id, var in ()]
    gateway_entries = [
        {
            "gateway_id": gateway_id,
            "endpoint": endpoint,
            "feedback_mapping": [
                {"device_id": device, "point_id": point, "variable_id": variable_id}
                for gateway_id_check, variable_id in mappings
                if gateway_id_check == gateway_id
            ],
        }
        for gateway_id, endpoint in sorted(gateways.items())
    ]
    return {
        "schema_version": 1,
        "file_version": "1.0.0",
        "scenarios": [
            {
                "scenario_id": scenario_id,
                "version": "1.0.0",
                "description": "acceptance generated scenario",
                "source": {
                    "kind": "simulation",
                    "interval_seconds": interval,
                    "seed": seed,
                },
                "devices": [
                    {
                        "device_id": device,
                        "points": [
                            {
                                "point_id": point,
                                "model": {
                                    "kind": "random",
                                    "distribution": "uniform",
                                    "minimum": minimum,
                                    "maximum": maximum,
                                },
                            }
                        ],
                    }
                ],
                "gateways": gateway_entries,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# 真实 Industrial Gateway
# --------------------------------------------------------------------------- #


def build_data_gateway_config(
    *,
    endpoint_id: str,
    data_bind: str,
    devices: dict[str, Sequence[tuple[str, str, str]]],
    data_writers: Sequence[str] = ("127.0.0.1",),
    journal_path: Path | None = None,
    control_enabled: bool = False,
    commander_endpoint: str = "127.0.0.1:9",
) -> GatewayConfig:
    """仅含 feedback 虚拟点的真实 Gateway 配置；变量名=设备.点。

    devices: 逻辑设备名 -> [(point_id, data_type, role), ...]
    """
    modbus_port = reserve_port()
    unique_points = sorted({point for points in devices.values() for point in points})
    points_table: dict[str, dict[str, Any]] = {}
    holding_cursor = 100
    coil_cursor = 0
    for point_id, data_type, role in unique_points:
        if data_type == "bool":
            address = {"register_type": "coil", "address": coil_cursor}
            coil_cursor += 1
        else:
            address = {"register_type": "holding", "address": holding_cursor}
            holding_cursor += 2 if data_type == "float32" else 1
        points_table[point_id] = {
            "role": role,
            "data_type": data_type,
            "address": address,
            **({"max_age_seconds": 30.0} if role == "feedback" else {}),
        }
    table = {"protocol": "modbus", "points": points_table}
    config: dict[str, Any] = {
        "data_grpc_bind": data_bind,
        "endpoints": {
            endpoint_id: {
                "protocol": "modbus",
                "host": "127.0.0.1",
                "port": modbus_port,
                "protocol_options": {"mode": "tcp"},
            }
        },
        "point_tables": {"dg_points": table},
        "devices": {
            device_id: {
                "endpoint_id": endpoint_id,
                "point_table_id": "dg_points",
                "protocol_address": {"unit_id": 1},
                "commands": {
                    point_id: {"device_id": f"phys-{device_id}", "point_id": point_id}
                    for point_id, _data_type, role in points
                    if role == "command"
                },
            }
            for device_id, points in devices.items()
        },
        "control": {
            "enabled": control_enabled,
            "commander": {"endpoint": commander_endpoint},
            "journal_path": str(journal_path or Path("/tmp/dg-acceptance-journal.json")),
            "access": {
                "data_writers": list(data_writers),
                "command_writers": [],
            },
        },
    }
    return GatewayConfig.model_validate(config)


@dataclass(slots=True)
class RealGateway:
    application: GatewayApplication
    endpoint_id: str
    data_bind: str

    async def stop(self) -> None:
        await self.application.stop()


@pytest.fixture
async def start_real_gateway() -> AsyncIterator[Callable[..., Awaitable[RealGateway]]]:
    running: list[RealGateway] = []

    async def factory(
        *,
        endpoint_id: str,
        devices: dict[str, Sequence[tuple[str, str, str]]] | None = None,
        data_bind: str | None = None,
        data_writers: Sequence[str] = ("127.0.0.1",),
        journal_path: Path | None = None,
    ) -> RealGateway:
        bind = data_bind or f"127.0.0.1:{reserve_port()}"
        if devices is None:
            devices = {"GW01": [("pitch_angle", "float32", "feedback")]}
        config = build_data_gateway_config(
            endpoint_id=endpoint_id,
            data_bind=bind,
            devices=devices,
            data_writers=data_writers,
            journal_path=journal_path,
        )
        application = assemble_gateway(config)
        await application.start()
        gateway = RealGateway(application, endpoint_id, bind)
        running.append(gateway)
        return gateway

    yield factory
    for gateway in reversed(running):
        await gateway.stop()


# --------------------------------------------------------------------------- #
# 真实 Data Generator 进程内 Server
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DataGeneratorHarness:
    runtime: Runtime
    server: grpc.aio.Server
    bind: str
    service_path: Path
    dg: Any  # DataGeneratorService stub
    common: Any  # CommonService stub
    channel: grpc.aio.Channel

    async def stop(self) -> None:
        await self.server.stop(grace=1.0)
        await self.channel.close()
        await self.runtime.shutdown()


@pytest.fixture
async def dg_server(
    tmp_path: Path,
) -> AsyncIterator[Callable[..., Awaitable[DataGeneratorHarness]]]:
    harnesses: list[DataGeneratorHarness] = []

    async def factory(service_path: Path, *, timeout: float | None = None) -> DataGeneratorHarness:
        recover_transaction(service_path.absolute().parent.parent)
        settings = load_settings(service_path)
        output = settings.service.get("output", {})
        writer = GatewayWriter(
            timeout=float(output.get("grpc_timeout_seconds", 3.0))
            if timeout is None
            else timeout,
            max_points=int(settings.service.get("buffer", {}).get("max_points_per_batch", 1000)),
            max_inflight=int(output.get("max_inflight", 1)),
        )
        runtime = Runtime(settings, writer)
        bind = settings.service.get("service", {}).get("grpc_bind", f"127.0.0.1:{reserve_port()}")
        server = make_server(runtime, bind)
        await server.start()
        channel = grpc.aio.insecure_channel(bind)
        await channel.channel_ready()
        harness = DataGeneratorHarness(
            runtime,
            server,
            bind,
            service_path,
            dg_rpc.DataGeneratorServiceStub(channel),
            common_rpc.CommonServiceStub(channel),
            channel,
        )
        harnesses.append(harness)
        return harness

    yield factory
    for harness in reversed(harnesses):
        await harness.stop()


# --------------------------------------------------------------------------- #
# 独立 Data Generator 子进程
# --------------------------------------------------------------------------- #


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


async def wait_for_port(bind: str, timeout: float = 15.0) -> None:
    host, port_text = bind.rsplit(":", 1)
    port = int(port_text)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await asyncio.to_thread(_port_open, host, port):
            await asyncio.sleep(0.15)
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"service did not bind {bind}")


@pytest.fixture
def dg_subprocess(tmp_path: Path):
    processes: list[subprocess.Popen[bytes]] = []

    async def factory(service_path: Path, *, wait_ready: bool = True) -> tuple[str, subprocess.Popen]:
        bind_text = yaml.safe_load(service_path.read_text(encoding="utf-8"))["service"][
            "grpc_bind"
        ]
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(SRC_PATH) + (os.pathsep + existing if existing else "")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "data_generator.main",
                "--config",
                str(service_path),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        processes.append(process)
        if wait_ready:
            await wait_for_port(bind_text)
        return bind_text, process

    yield factory

    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@asynccontextmanager
async def grpc_channel(bind: str) -> AsyncIterator[grpc.aio.Channel]:
    channel = grpc.aio.insecure_channel(bind)
    await channel.channel_ready()
    try:
        yield channel
    finally:
        await channel.close()
