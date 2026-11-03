"""Real-process fixtures for Collector v1 system acceptance.

Boundaries (design §13.2/§14.2):
- Collector runs as an independent process launched from the real ``wind-hub``
  CLI entry (``python -m collector.main``), exercised over real gRPC.
- Modbus/IEC104 peers are real protocol stacks (pymodbus server, c104/lib60870
  slave) bound to TCP loopback.
- NATS/JetStream is a real container/server. When no real broker is available,
  dependent tests are NOT_RUN via skip with an explicit reason; they never fall
  back to an in-process fake.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import grpc
import pytest
import pytest_asyncio

from contracts import collector_pb2_grpc, common_pb2_grpc
from contracts.collector_pb2_grpc import CollectorServiceStub
from contracts.common_pb2_grpc import CommonServiceStub
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer

from .config_tree import write_config_tree

ClientsFactory = Callable[[int], tuple[grpc.aio.Channel, CommonServiceStub, CollectorServiceStub]]

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_TOKEN = "acceptance-admin-token"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag protocol-parameterized cases (ids look like ``test_x[modbus]``)."""
    for item in items:
        existing = {marker.name for marker in item.iter_markers()}
        parameter_id = item.name.rsplit("[", 1)[1][:-1] if "[" in item.name else ""
        for protocol in ("modbus", "iec104"):
            if protocol not in existing and parameter_id.startswith(protocol):
                item.add_marker(getattr(pytest.mark, protocol))


# ---------------------------------------------------------------------------
# Real NATS/JetStream (docker-managed; never faked)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RealNats:
    url: str
    container: str
    client_port: int


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def real_nats() -> Iterator[RealNats]:
    """A real NATS/JetStream broker. NOT_RUN when docker/image is unavailable."""
    if not _docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable for real NATS/JetStream")
    port = free_port()
    container = f"collector-v1-acceptance-nats-{uuid.uuid4().hex[:8]}"
    run = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"{port}:4222",
            "nats:2.10-alpine",
            "-js",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if run.returncode != 0:
        pytest.skip(f"NOT_RUN: real NATS container unavailable: {run.stderr.strip()}")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
        pytest.skip("NOT_RUN: real NATS did not open its client port")
    try:
        yield RealNats(f"nats://127.0.0.1:{port}", container, port)
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)


# ---------------------------------------------------------------------------
# Real protocol slaves
# ---------------------------------------------------------------------------


def _modbus_holding() -> list[int]:
    """Register block (pymodbus addresses start at 1) matching the test tree."""
    from pymodbus.client import AsyncModbusTcpClient

    datatype = AsyncModbusTcpClient.DATATYPE
    values = [0] * 256
    words = AsyncModbusTcpClient.convert_to_registers(1500, datatype.INT32, word_order="little")
    values[0:2] = words
    values[2:4] = AsyncModbusTcpClient.convert_to_registers(
        12.5, datatype.FLOAT32, word_order="little"
    )
    values[4] = 42
    return values


@pytest_asyncio.fixture
async def modbus_slave() -> AsyncIterator[ModbusMockServer]:
    server = ModbusMockServer(port=free_port(), holding=_modbus_holding())
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest_asyncio.fixture
async def iec104_slave() -> AsyncIterator[IEC104MockServer]:
    server = IEC104MockServer(
        port=free_port(),
        stations={1: {100: 1500.5, 200: 50.0}},
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Real Collector process
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CollectorProcess:
    process: subprocess.Popen[str]
    grpc_port: int
    config_root: Path
    state_dir: Path
    log: Path

    def stop(self, *, timeout: float = 15.0) -> int:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                exit_code = self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                exit_code = self.process.wait(timeout=5)
        else:
            exit_code = int(self.process.returncode or 0)
        return exit_code

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


async def _wait_grpc(port: int, timeout: float = 20.0) -> None:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=timeout)
    finally:
        await channel.close()


@pytest_asyncio.fixture
async def collector(
    tmp_path: Path,
    modbus_slave: ModbusMockServer,
    iec104_slave: IEC104MockServer,
    real_nats: RealNats,
) -> AsyncIterator[Callable[..., Any]]:
    """Launch a CollectorProcess; tests pull a typed handle via ``start_collector``."""
    handles: list[CollectorHandle] = []

    async def start_collector(**tree_kwargs: Any) -> CollectorHandle:
        config_root = tmp_path / f"cfg-{uuid.uuid4().hex[:8]}"
        iec_port = int(tree_kwargs.pop("iec104_port_override", 0)) or iec104_slave.port
        modbus_port = int(tree_kwargs.pop("modbus_port_override", 0)) or modbus_slave.port
        state_override = tree_kwargs.pop("state_dir_override", None)
        services = write_config_tree(
            config_root,
            modbus_port=modbus_port,
            iec104_port=iec_port,
            nats_servers=[real_nats.url],
            **tree_kwargs,
        )
        state_dir = (
            Path(str(state_override))
            if state_override is not None
            else tmp_path / f"state-{uuid.uuid4().hex[:8]}"
        )
        grpc_port = free_port()
        log_path = tmp_path / "collector.log"
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONFAULTHANDLER": "1",
            "WIND_HUB_COLLECTOR_ADMIN_TOKEN": ADMIN_TOKEN,
        }
        log_stream = log_path.open("w", encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "collector.main",
            "--config",
            str(services / "collector.yaml"),
            "--state-directory",
            str(state_dir),
            "--grpc-port",
            str(grpc_port),
            "--shutdown-timeout",
            "8",
        ]
        process = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            cmd,
            cwd=REPO_ROOT,
            env=env,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            await _wait_grpc(grpc_port)
        except Exception:
            if process.poll() is not None:
                log_stream.close()
                pytest.fail(f"Collector exited before gRPC readiness:\n{log_path.read_text()}")
            process.kill()
            log_stream.close()
            raise
        handle = CollectorHandle(
            CollectorProcess(process, grpc_port, config_root, state_dir, log_path),
            services,
            config_root / "instances",
            config_root / "templates",
            state_dir,
            log_stream,
        )
        handles.append(handle)
        return handle

    yield start_collector

    for handle in handles:
        if handle.process.process.poll() is None:
            handle.process.stop()
        handle.log_stream.close()


@dataclass(slots=True)
class CollectorHandle:
    process: CollectorProcess
    services: Path
    instances: Path
    templates: Path
    state_dir: Path
    log_stream: TextIO


# ---------------------------------------------------------------------------
# gRPC clients
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def grpc_clients() -> AsyncIterator[ClientsFactory]:
    opened: list[grpc.aio.Channel] = []

    def clients(port: int) -> tuple[grpc.aio.Channel, CommonServiceStub, CollectorServiceStub]:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        opened.append(channel)
        return (
            channel,
            common_pb2_grpc.CommonServiceStub(channel),
            collector_pb2_grpc.CollectorServiceStub(channel),
        )

    try:
        yield clients
    finally:
        for channel in opened:
            await channel.close()


ADMIN_METADATA = (("x-windhub-admin-token", ADMIN_TOKEN),)
