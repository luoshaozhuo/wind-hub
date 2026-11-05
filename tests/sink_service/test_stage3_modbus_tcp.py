"""Gate-4 Modbus TCP system test against a real pymodbus client.

The Modbus sink is a read-only Modbus TCP *server* owned by the realtime
process. This test boots the two real sink OS processes over mTLS, publishes a
real NATS Core measurement, and then connects a real pymodbus client to read
the holding registers (function code 3) that the sink exposed. No Modbus
behaviour is mocked; only the NATS broker and OS processes stand in as
infrastructure, both real.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import grpc
import pytest

from contracts import common_pb2, common_pb2_grpc
from contracts.sample_message import SampleMessage
from sink_service.application.config import validate_snapshot
from sink_service.domain.config import SnapshotFile
from sink_service.infrastructure.config_repository import GenerationStore
from tests.fixtures.services.r5_stack import R5Stack, docker_available
from tests.sink_service._stage1_fixtures import (
    REALTIME_SERVICE,
    RELIABLE_SERVICE,
    TEMPLATE_ROOT,
    snapshot_docs,
    write_tree,
)
from tests.sink_service._stage1_tls import write_mtls_bundle

pytestmark = [pytest.mark.system, pytest.mark.integration, pytest.mark.modbus]

REPO_ROOT = Path(__file__).resolve().parents[2]
_CALLERS = [
    {
        "caller_id": "maintenance",
        "certificate_identity": "spiffe://wind-hub/maintenance",
        "permissions": ["query", "control", "config"],
    },
]
MODBUS_TEMPLATE_PATH = f"{TEMPLATE_ROOT}/sinks/modbus_server_1.0.0.yaml"
UNIT_ID = 1
VALUE_START = 0
QUALITY_START = 100


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(frozen=True)
class _Env:
    root: Path
    bundle: dict[str, Path]
    writer_port: int
    reader_port: int
    modbus_port: int


def _stack() -> R5Stack:
    try:
        return R5Stack(
            project=os.environ["SINK_R5_PROJECT"],
            nats=os.environ["SINK_R5_NATS"],
            redis=os.environ["SINK_R5_REDIS"],
            tdengine_rest=os.environ["SINK_R5_TDENGINE"],
            kafka=os.environ["SINK_R5_KAFKA"],
            nats_monitor_port=int(os.environ["SINK_R5_NATS_MON_PORT"]),
        )
    except KeyError:
        if not docker_available():
            pytest.skip("docker unavailable and no external SINK_R5_* stack provided")
        pytest.skip("Modbus Gate-4 test runs against an external pinned stack (SINK_R5_*)")
        raise  # pragma: no cover


@pytest.fixture
def stack() -> R5Stack:
    return _stack()


@pytest.fixture
def env(tmp_path: Path, stack: R5Stack) -> _Env:
    writer_port, reader_port, modbus_port = _free_port(), _free_port(), _free_port()
    bundle = write_mtls_bundle(tmp_path)
    value = _Env(tmp_path, bundle, writer_port, reader_port, modbus_port)

    docs = snapshot_docs(
        writer_port=writer_port,
        reader_port=reader_port,
        config_root=str(tmp_path),
        callers=_CALLERS,
        include_templates=("realtime_power",),
        sinks={
            "ems_modbus": {
                "template_id": "modbus_server",
                "template_version": "1.0.0",
                "connection": {"listen_host": "127.0.0.1", "port": modbus_port},
                "overrides": {},
            }
        },
        deliveries={
            "ems_power": {
                "template_id": "realtime_power",
                "template_version": "1.0.0",
                "sink_id": "ems_modbus",
                "desired_state": "START",
                "source": {
                    "wind_farm_id": "farm-a",
                    "collector_id": "collector-1",
                    "devices": ["WT001"],
                    "points": ["active_power"],
                },
                "overrides": {},
            }
        },
    )
    docs[MODBUS_TEMPLATE_PATH] = {
        "schema_version": 1,
        "file_version": "1.0.0",
        "template_id": "modbus_server",
        "object_version": "1.0.0",
        "kind": "modbus_tcp",
        "capabilities": {
            "realtime": True,
            "reliable": False,
            "idempotent": False,
            "supports_concurrent_write": False,
            "max_safe_inflight": 1,
        },
        "connection": {},
        "output": {
            "write_timeout_ms": 2000,
            "value_encoding": "float32_be",
            "quality_encoding": "separate_register",
            "quality_codes": {"good": 0, "bad": 1, "uncertain": 2},
            "unit_id": UNIT_ID,
            "register_start": VALUE_START,
            "quality_register_start": QUALITY_START,
        },
    }
    for service in (REALTIME_SERVICE, RELIABLE_SERVICE):
        docs[service]["nats"]["servers"] = [stack.nats]
    for name in ("sink-realtime", "sink-reliable"):
        docs[f"configs/services/{name}.yaml"]["security"].update(
            ca_cert=str(bundle["ca"]),
            server_cert=str(bundle["server_crt"]),
            server_key=str(bundle["server_key"]),
        )
    docs[RELIABLE_SERVICE]["config_sync"]["writer_endpoint"] = f"127.0.0.1:{writer_port}"
    write_tree(tmp_path, docs)
    snapshot = validate_snapshot(
        tuple(
            SnapshotFile(str(path.relative_to(tmp_path)), path.read_bytes())
            for path in sorted(tmp_path.rglob("*.yaml"))
        )
    )
    GenerationStore(tmp_path).commit(snapshot, "", old=None)
    return value


def _popen(env: _Env, service: str, peer: str, log_name: str) -> subprocess.Popen[str]:
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    handle = open(env.root / log_name, "w", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "sink_service.main",
            "--config-root",
            str(env.root),
            "--service-file",
            service,
            "--peer-service-file",
            peer,
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc._log_handle = handle  # type: ignore[attr-defined]
    return proc


@pytest.fixture
def processes(env: _Env):
    writer = _popen(
        env,
        "configs/services/sink-realtime.yaml",
        "configs/services/sink-reliable.yaml",
        "writer.log",
    )
    reader = _popen(
        env,
        "configs/services/sink-reliable.yaml",
        "configs/services/sink-realtime.yaml",
        "reader.log",
    )
    try:
        yield writer, reader
    finally:
        for proc in (writer, reader):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (writer, reader):
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _channel(env: _Env, port: int) -> grpc.aio.Channel:
    credentials = grpc.ssl_channel_credentials(
        root_certificates=env.bundle["ca"].read_bytes(),
        private_key=env.bundle["admin_key"].read_bytes(),
        certificate_chain=env.bundle["admin_crt"].read_bytes(),
    )
    return grpc.aio.secure_channel(
        f"127.0.0.1:{port}",
        credentials,
        options=(("grpc.ssl_target_name_override", "localhost"),),
    )


async def _wait_ready(env: _Env, port: int, *, role: str) -> None:
    deadline = time.monotonic() + 30.0
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            async with _channel(env, port) as channel:
                info = await asyncio.wait_for(
                    common_pb2_grpc.CommonServiceStub(channel).GetServiceInfo(
                        common_pb2.Empty(), timeout=2
                    ),
                    timeout=3,
                )
            if info.process_role == role:
                return
            last = RuntimeError(f"role {info.process_role!r}, expected {role!r}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        await asyncio.sleep(0.25)
    pytest.fail(f"process on {port} never became ready: {last}")


async def _wait_delivery(env: _Env, port: int, delivery: str) -> None:
    deadline = time.monotonic() + 30.0
    last: str | None = None
    while time.monotonic() < deadline:
        async with _channel(env, port) as channel:
            status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
                common_pb2.Empty()
            )
        dep = {item.name: item.health for item in status.dependencies}
        detail = {item.name: item.detail for item in status.dependencies}
        last = dep.get(delivery)
        if last == "RUNNING":
            return
        if last == "FAILED":
            log = (env.root / "writer.log").read_text(encoding="utf-8", errors="replace")
            pytest.fail(f"{delivery} FAILED: {detail.get(delivery)!r}\n{log[-3000:]}")
        await asyncio.sleep(0.3)
    pytest.fail(f"{delivery} stuck at {last!r}")


async def test_modbus_server_serves_register_image_over_real_tcp(
    env: _Env, stack: R5Stack, processes
) -> None:
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")
    await _wait_delivery(env, env.writer_port, "ems_power")

    now = datetime.now(UTC)
    sample = SampleMessage(
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-1",
        device_id="WT001",
        point_id="active_power",
        sample_id="mb-0001",
        value=35000.0,
        quality="good",
        sample_time=now,
        publish_time=now,
        max_staleness_ms=60000,
    )
    import nats

    client = await nats.connect(servers=stack.nats, connect_timeout=5)
    try:
        await client.publish(sample.subject("realtime"), sample.encode())
        await client.flush()
    finally:
        await client.drain()

    # Connect a real Modbus master and read the float32 image via function 3.
    from pymodbus.client import AsyncModbusTcpClient

    modbus = AsyncModbusTcpClient("127.0.0.1", port=env.modbus_port)
    await modbus.connect()
    try:
        deadline = time.monotonic() + 12.0
        decoded = None
        while time.monotonic() < deadline:
            value_response = await modbus.read_holding_registers(
                address=VALUE_START, count=2, device_id=UNIT_ID
            )
            if not value_response.isError() and value_response.registers:
                high, low = value_response.registers
                decoded = struct.unpack(">f", struct.pack(">HH", high, low))[0]
                if decoded == pytest.approx(35000.0, abs=0.01):
                    break
            await asyncio.sleep(0.25)
        assert decoded is not None and decoded == pytest.approx(35000.0, abs=0.01)

        quality_response = await modbus.read_holding_registers(
            address=QUALITY_START, count=1, device_id=UNIT_ID
        )
        assert not quality_response.isError()
        assert quality_response.registers[0] == 0  # good -> coil value 0

        # External writes are refused by the read-only Modbus context.
        refused = await modbus.write_register(VALUE_START, 7, device_id=UNIT_ID)
        assert refused.isError()
    finally:
        modbus.close()

    for proc in (writer, reader):
        proc.send_signal(signal.SIGTERM)
    for proc in (writer, reader):
        try:
            assert proc.wait(timeout=15) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
