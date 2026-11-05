"""Gate-4 real-infrastructure system tests (no mocks for system components).

These tests bring up the pinned R5 stack (NATS JetStream / Redis / Kafka /
TDengine) through :mod:`tests.fixtures.services.r5_stack`, boot the two real
``sink_service.main`` OS processes over mutual TLS, and drive data across real
brokers. Every dependency here is a real container or a real OS process; there
are no test doubles standing in for system infrastructure.

Scenario coverage is expanded incrementally. This module currently pins the
foundational realtime path and the S2-1 unblock: with NATS reachable, a START
intent must no longer block gRPC bring-up, and a real NATS Core sample must land
in Redis through the atomic CAS writer.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
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
from tests.fixtures.services.r5_stack import (
    R5Stack,
    R5StackError,
    docker_available,
    down_stack,
    up_stack,
)
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    REALTIME_SERVICE,
    RELIABLE_SERVICE,
    SINKS_INSTANCE,
    snapshot_docs,
    write_tree,
)
from tests.sink_service._stage1_tls import write_mtls_bundle

pytestmark = [pytest.mark.system, pytest.mark.integration, pytest.mark.reliability]

REPO_ROOT = Path(__file__).resolve().parents[2]

_CALLERS = [
    {
        "caller_id": "maintenance",
        "certificate_identity": "spiffe://wind-hub/maintenance",
        "permissions": ["query", "control", "config"],
    },
]


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


@pytest.fixture
def stack() -> R5Stack:
    # External lifecycle mode: when the pinned stack is already running (its
    # connection params supplied via environment), the test never orchestrates
    # docker itself -- it only connects to the real brokers. Otherwise the
    # fixture owns an isolated R5 project's full up/down lifecycle.
    external = os.environ.get("SINK_R5_NATS")
    if external:
        yield R5Stack(
            project=os.environ.get("SINK_R5_PROJECT", "wind-hub-r5-manual"),
            nats=external,
            redis=os.environ["SINK_R5_REDIS"],
            tdengine_rest=os.environ["SINK_R5_TDENGINE"],
            kafka=os.environ["SINK_R5_KAFKA"],
            nats_monitor_port=int(os.environ["SINK_R5_NATS_MON_PORT"]),
        )
        return
    if not docker_available():
        pytest.skip("docker daemon unavailable; real-infra Gate-4 tests cannot run")
    value = up_stack()
    try:
        yield value
    finally:
        down_stack(value)


@pytest.fixture
def env(tmp_path: Path, stack: R5Stack) -> _Env:
    writer_port, reader_port = _free_port(), _free_port()
    bundle = write_mtls_bundle(tmp_path)
    value = _Env(tmp_path, bundle, writer_port, reader_port)
    docs = snapshot_docs(
        writer_port=writer_port,
        reader_port=reader_port,
        config_root=str(tmp_path),
        callers=_CALLERS,
    )
    # Point both processes at the real NATS Core/JetStream endpoint.
    for service in (REALTIME_SERVICE, RELIABLE_SERVICE):
        docs[service]["nats"]["servers"] = [stack.nats]
    # Bind the realtime Redis sink to the live container's loopback mapping.
    redis_port = int(stack.redis.rsplit(":", 1)[1])
    docs[SINKS_INSTANCE]["sinks"]["ems_redis"]["connection"] = {
        "host": "127.0.0.1",
        "port": redis_port,
    }
    # Reliable Kafka delivery stays STOPPED for this realtime-only milestone.
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "START"
    docs[DELIVERIES_INSTANCE]["deliveries"]["power_history"]["desired_state"] = "STOP"
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


@pytest.fixture
def processes(env: _Env):
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    base = [sys.executable, "-m", "sink_service.main", "--config-root", str(env.root)]
    writer = subprocess.Popen(
        [
            *base,
            "--service-file",
            "configs/services/sink-realtime.yaml",
            "--peer-service-file",
            "configs/services/sink-reliable.yaml",
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    reader = subprocess.Popen(
        [
            *base,
            "--service-file",
            "configs/services/sink-reliable.yaml",
            "--peer-service-file",
            "configs/services/sink-realtime.yaml",
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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


def _redis_get(stack: R5Stack, key: str) -> str:
    result = stack.docker("exec", stack.container("redis"), "redis-cli", "GET", key)
    if result.returncode != 0:
        raise R5StackError(f"redis-cli GET failed: {result.stderr.strip()}")
    return result.stdout.strip()


async def test_realtime_start_unblocks_and_nats_sample_lands_in_redis(
    env: _Env, stack: R5Stack, processes
) -> None:
    writer, reader = processes

    # S2-1 unblock: with NATS reachable, a START intent must not block gRPC
    # bring-up. Both processes bind and answer over mutual TLS promptly.
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")

    async with _channel(env, env.writer_port) as channel:
        stub = common_pb2_grpc.CommonServiceStub(channel)
        status = await stub.GetServiceStatus(common_pb2.Empty())
        assert status.health in ("HEALTHY", "DEGRADED")
        dependency = {item.name: item.health for item in status.dependencies}
        # The realtime delivery reached a running, non-failed state for real.
        assert dependency["ems_power"] == "RUNNING"

    # Publish a real NATS Core measurement on the contract subject.
    now = datetime.now(UTC)
    sample = SampleMessage(
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-1",
        device_id="WT001",
        point_id="active_power",
        sample_id="s-0001",
        value=35000.0,
        quality="good",
        sample_time=now,
        publish_time=now,
        max_staleness_ms=5000,
    )
    import nats

    client = await nats.connect(servers=stack.nats, connect_timeout=5)
    try:
        await client.publish(sample.subject("realtime"), sample.encode())
        await client.flush()
    finally:
        await client.drain()

    # The change_with_heartbeat engine emits the first observed value; the Redis
    # CAS writer must persist it under the namespace-prefixed point key.
    key = "windhub:farm-a:collector-1:WT001:active_power"
    deadline = time.monotonic() + 12.0
    body = ""
    while time.monotonic() < deadline:
        body = _redis_get(stack, key)
        if body:
            break
        await asyncio.sleep(0.25)
    assert body, f"realtime sample never persisted to Redis key {key}"
    assert '"sample_id":"s-0001"' in body
    assert "35000.0" in body or "35000" in body

    # Both processes leave SIGTERM cleanly with a zero exit code.
    for proc in (writer, reader):
        proc.send_signal(signal.SIGTERM)
    for proc in (writer, reader):
        try:
            assert proc.wait(timeout=15) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            raise


async def _publish_realtime_sample(stack: R5Stack, sample: SampleMessage) -> None:
    import nats

    client = await nats.connect(servers=stack.nats, connect_timeout=5)
    try:
        await client.publish(sample.subject("realtime"), sample.encode())
        await client.flush()
    finally:
        await client.drain()


async def test_realtime_recovers_after_nats_broker_pause(
    env: _Env, stack: R5Stack, processes
) -> None:
    """A real NATS broker freeze must not kill the processes; reconnect restores delivery."""
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")

    async with _channel(env, env.writer_port) as channel:
        status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
            common_pb2.Empty()
        )
        assert {item.name: item.health for item in status.dependencies}["ems_power"] == "RUNNING"

    # Freeze the real broker container (SIGSTOP): TCP connections hang, exactly
    # the condition the unlimited-reconnect ingress must survive.
    stack.pause("nats")
    try:
        # The control plane stays alive and answering throughout the outage.
        await asyncio.sleep(5.0)
        async with _channel(env, env.writer_port) as channel:
            info = await common_pb2_grpc.CommonServiceStub(channel).GetServiceInfo(
                common_pb2.Empty()
            )
        assert info.process_role == "realtime"
        assert writer.poll() is None and reader.poll() is None
    finally:
        stack.unpause("nats")

    # After the broker resumes, a fresh measurement must flow end to end again.
    now = datetime.now(UTC)
    recovered = SampleMessage(
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-1",
        device_id="WT001",
        point_id="active_power",
        sample_id="s-recover-1",
        value=36000.0,
        quality="good",
        sample_time=now,
        publish_time=now,
        max_staleness_ms=60000,
    )
    await _publish_realtime_sample(stack, recovered)

    key = "windhub:farm-a:collector-1:WT001:active_power"
    deadline = time.monotonic() + 20.0
    body = ""
    while time.monotonic() < deadline:
        body = _redis_get(stack, key)
        if '"sample_id":"s-recover-1"' in body:
            break
        await asyncio.sleep(0.5)
    assert '"sample_id":"s-recover-1"' in body, f"post-reconnect sample missing: {body!r}"

    async with _channel(env, env.writer_port) as channel:
        status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
            common_pb2.Empty()
        )
        assert {item.name: item.health for item in status.dependencies}["ems_power"] == "RUNNING"

    for proc in (writer, reader):
        proc.send_signal(signal.SIGTERM)
    for proc in (writer, reader):
        try:
            assert proc.wait(timeout=15) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            raise


async def test_realtime_same_timestamp_conflict_keeps_first_identity(
    env: _Env, stack: R5Stack, processes
) -> None:
    """Same sample_time with a different sample_id must never overwrite the first value."""
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")

    conflict_time = datetime.now(UTC)
    first = SampleMessage(
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-1",
        device_id="WT002",
        point_id="active_power",
        sample_id="s-cas-first",
        value=111.1,
        quality="good",
        sample_time=conflict_time,
        publish_time=conflict_time,
        max_staleness_ms=60000,
    )
    second = SampleMessage(
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-1",
        device_id="WT002",
        point_id="active_power",
        sample_id="s-cas-second",
        value=222.2,
        quality="good",
        sample_time=conflict_time,  # identical timestamp, different identity
        publish_time=conflict_time,
        max_staleness_ms=60000,
    )
    await _publish_realtime_sample(stack, first)

    key = "windhub:farm-a:collector-1:WT002:active_power"
    deadline = time.monotonic() + 12.0
    body = ""
    while time.monotonic() < deadline:
        body = _redis_get(stack, key)
        if '"sample_id":"s-cas-first"' in body:
            break
        await asyncio.sleep(0.25)
    assert '"sample_id":"s-cas-first"' in body, body

    # The conflicting identity must be rejected by the engine's monotonic guard.
    # Read within the engine-clamped 3 s TTL (max_age_ms wins over the 60 s
    # upstream staleness), so the assertion targets identity, not key expiry.
    await _publish_realtime_sample(stack, second)
    await asyncio.sleep(1.0)
    body_after = _redis_get(stack, key)
    assert '"sample_id":"s-cas-first"' in body_after
    assert "s-cas-second" not in body_after
    assert "222.2" not in body_after and "222.20000" not in body_after

    async with _channel(env, env.writer_port) as channel:
        response = await common_pb2_grpc.CommonServiceStub(channel).GetMetrics(
            common_pb2.GetMetricsRequest(window_ms=60000)
        )
    metrics = {item.name.rsplit(".", 1)[-1]: item.value for item in response.metrics}
    assert metrics.get("dropped_total", 0.0) >= 1.0

    for proc in (writer, reader):
        proc.send_signal(signal.SIGTERM)
    for proc in (writer, reader):
        try:
            assert proc.wait(timeout=15) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
