"""Gate-4 reliable JetStream system tests against the real pinned R5 stack.

The reliable process runs a durable JetStream pull consumer whose target is the
real Kafka broker. Every dependency is real: NATS JetStream streams and
consumers, a Kafka producer owned by the sink process and a Kafka consumer used
to independently verify delivery, plus two real sink OS processes over mTLS.

Streams are provisioned through the real JetStream administrative API (the
role an operator plays in production -- the sink itself only validates and
binds, never creates them). No component is mocked.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from tests.fixtures.services.r5_stack import R5Stack, docker_available
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
HISTORY_STREAM = "WH_HISTORY"
DLQ_STREAM = "WH_DLQ"
KAFKA_TOPIC = "wh.history"


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
        pytest.skip("reliable Gate-4 tests run against an external pinned stack (SINK_R5_*)")
        raise  # pragma: no cover


@pytest.fixture
def stack() -> R5Stack:
    return _stack()


async def _provision_streams(nats_url: str) -> None:
    """Create (or recreate clean) the history and DLQ JetStream streams."""
    import nats
    from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

    client = await nats.connect(servers=nats_url, connect_timeout=5)
    try:
        js = client.jetstream()
        for name in (HISTORY_STREAM, DLQ_STREAM):
            with contextlib.suppress(Exception):
                await js.delete_stream(name)
        await js.add_stream(
            StreamConfig(
                name=HISTORY_STREAM,
                subjects=["wh.hist.>"],
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.NEW,
                max_bytes=256 * 1024 * 1024,
                max_age=2 * 3600.0,
            )
        )
        # The sink enforces a DLQ capacity floor of dlq_max_bytes (1 GiB) and a
        # max-age floor of dlq_max_age_seconds (86400 s); honour both exactly.
        await js.add_stream(
            StreamConfig(
                name=DLQ_STREAM,
                subjects=["wh.dlq"],
                storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS,
                discard=DiscardPolicy.NEW,
                max_bytes=1024 * 1024 * 1024,
                max_age=86400.0,
            )
        )
    finally:
        await client.drain()


@pytest.fixture
def env(tmp_path: Path, stack: R5Stack):
    writer_port, reader_port = _free_port(), _free_port()
    bundle = write_mtls_bundle(tmp_path)
    value = _Env(tmp_path, bundle, writer_port, reader_port)
    docs = snapshot_docs(
        writer_port=writer_port,
        reader_port=reader_port,
        config_root=str(tmp_path),
        callers=_CALLERS,
    )
    for service in (REALTIME_SERVICE, RELIABLE_SERVICE):
        docs[service]["nats"]["servers"] = [stack.nats]
        # ReliableRuntime reads these keys directly; supply explicit timeouts.
        docs[service]["nats"]["connect_timeout_ms"] = 2000
        docs[service]["nats"]["max_reconnect_interval_ms"] = 1000
    kafka_port = int(stack.kafka.rsplit(":", 1)[1])
    docs[SINKS_INSTANCE]["sinks"]["history_kafka"]["connection"] = {
        "bootstrap_servers": [f"127.0.0.1:{kafka_port}"]
    }
    # Only the reliable Kafka delivery runs; the realtime Redis delivery is STOP.
    docs[DELIVERIES_INSTANCE]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    docs[DELIVERIES_INSTANCE]["deliveries"]["power_history"]["desired_state"] = "START"
    for name in ("sink-realtime", "sink-reliable"):
        docs[f"configs/services/{name}.yaml"]["security"].update(
            ca_cert=str(bundle["ca"]),
            server_cert=str(bundle["server_crt"]),
            server_key=str(bundle["server_key"]),
        )
    docs[RELIABLE_SERVICE]["config_sync"]["writer_endpoint"] = f"127.0.0.1:{writer_port}"
    asyncio.run(_provision_streams(stack.nats))
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


def _spawn_writer(env: _Env) -> subprocess.Popen[str]:
    return _popen(
        env,
        "configs/services/sink-realtime.yaml",
        "configs/services/sink-reliable.yaml",
        "writer.log",
    )


def _spawn_reader(env: _Env) -> subprocess.Popen[str]:
    return _popen(
        env,
        "configs/services/sink-reliable.yaml",
        "configs/services/sink-realtime.yaml",
        "reader.log",
    )


def _log_tail(env: _Env, log_name: str, chars: int = 4000) -> str:
    path = env.root / log_name
    if not path.exists():
        return "<no log>"
    return path.read_text(encoding="utf-8", errors="replace")[-chars:]


def _spawn(env: _Env) -> tuple[subprocess.Popen[str], subprocess.Popen[str]]:
    return _spawn_writer(env), _spawn_reader(env)


def _terminate(writer: subprocess.Popen[str], reader: subprocess.Popen[str]) -> None:
    for proc in (writer, reader):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in (writer, reader):
        try:
            assert proc.wait(timeout=15) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            raise


@pytest.fixture
def processes(env: _Env):
    writer, reader = _spawn(env)
    try:
        yield writer, reader
    finally:
        _terminate(writer, reader)


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


async def _wait_delivery(
    env: _Env,
    port: int,
    delivery: str,
    expected: str = "RUNNING",
    timeout: float = 30.0,
) -> None:
    """Poll GetServiceStatus until a delivery settles; dump the log on failure."""
    deadline = time.monotonic() + timeout
    last: str | None = None
    detail = ""
    while time.monotonic() < deadline:
        async with _channel(env, port) as channel:
            status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
                common_pb2.Empty()
            )
        dep = {item.name: item.health for item in status.dependencies}
        details = {item.name: item.detail for item in status.dependencies}
        last = dep.get(delivery)
        detail = details.get(delivery, "")
        if last == expected:
            return
        if last == "FAILED":
            pytest.fail(
                f"{delivery} settled FAILED (want {expected}); detail={detail!r}\n"
                f"{_log_tail(env, 'reader.log')}"
            )
        await asyncio.sleep(0.3)
    pytest.fail(
        f"{delivery} stuck at {last!r}, want {expected}; detail={detail!r}\n"
        f"{_log_tail(env, 'reader.log')}"
    )


def _reliable_sample(sample_id: str) -> SampleMessage:
    now = datetime.now(UTC)
    return SampleMessage(
        wind_farm_id="farm-a",
        collector_id="collector-1",
        task_id="task-9",
        device_id="WT001",
        point_id="active_power",
        sample_id=sample_id,
        value=35000.0,
        quality="good",
        sample_time=now,
        publish_time=now,  # reliable messages must omit max_staleness_ms
    )


async def _jetstream_publish(nats_url: str, sample: SampleMessage) -> None:
    import nats

    client = await nats.connect(servers=nats_url, connect_timeout=5)
    try:
        ack = await client.jetstream().publish(sample.subject("reliable"), sample.encode())
        assert ack.stream == HISTORY_STREAM
    finally:
        await client.drain()


async def _consumer_info(stack: R5Stack, env: _Env):
    import hashlib

    import nats

    # Same deterministic seed as ReliableDeliveryPort._durable_id
    # (deployment_id "prod-a" from the shared service template).
    name = "WH_SINK_" + hashlib.sha256(b"prod-a:power_history").hexdigest()[:24]
    client = await nats.connect(servers=stack.nats, connect_timeout=5)
    try:
        js = client.jetstream()
        return await js.consumer_info(HISTORY_STREAM, name), name
    finally:
        await client.drain()


async def _kafka_drain(stack: R5Stack, min_messages: int, timeout: float = 30.0) -> list[bytes]:
    import aiokafka

    consumer = aiokafka.AIOKafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=stack.kafka,
        group_id=f"gate4-verify-{time.monotonic_ns()}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        consumer_timeout_ms=int(timeout * 1000),
    )
    await consumer.start()
    try:
        records: list[bytes] = []
        deadline = time.monotonic() + timeout
        while len(records) < min_messages and time.monotonic() < deadline:
            batch = await consumer.getmany(timeout_ms=1000, max_records=100)
            for messages in batch.values():
                for message in messages:
                    records.append(message.value)
        return records
    finally:
        await consumer.stop()


async def test_reliable_jetstream_acks_and_kafka_receives(
    env: _Env, stack: R5Stack, processes
) -> None:
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")

    # The reliable port binds Kafka + JetStream asynchronously; wait for the
    # settled RUNNING adjudication rather than sampling the transient STARTING.
    await _wait_delivery(env, env.reader_port, "power_history")

    sample = _reliable_sample("r-0001")
    await _jetstream_publish(stack.nats, sample)

    records = await _kafka_drain(stack, min_messages=1)
    assert any(b'"sample_id":"r-0001"' in body for body in records), records

    # The durable consumer must have explicitly ACKed the source message.
    deadline = time.monotonic() + 15.0
    info = None
    while time.monotonic() < deadline:
        info, _name = await _consumer_info(stack, env)
        if info.num_pending == 0 and info.num_ack_pending == 0:
            break
        await asyncio.sleep(0.25)
    assert info is not None and info.num_pending == 0 and info.num_ack_pending == 0
    # nats-py 2.16 exposes delivery counters on ConsumerInfo.delivered.
    assert info.delivered.stream_seq >= 1

    _terminate(writer, reader)


async def test_reliable_malformed_message_is_dead_lettered_and_acked(
    env: _Env, stack: R5Stack, processes
) -> None:
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")
    await _wait_delivery(env, env.reader_port, "power_history")

    import nats

    client = await nats.connect(servers=stack.nats, connect_timeout=5)
    try:
        js = client.jetstream()
        # Not parseable as a SampleMessage -> permanent failure -> DLQ path.
        ack = await js.publish("wh.hist.farm-a.collector-1.WT001.active_power", b'{"sample_id":')
        assert ack.stream == HISTORY_STREAM
    finally:
        await client.drain()

    # Wait for the record to land in the real DLQ stream, using a pull consumer
    # (PushSubscription has no fetch in nats-py 2.16).
    client = await nats.connect(servers=stack.nats, connect_timeout=5)
    dlq_payloads: list[bytes] = []
    try:
        js = client.jetstream()
        sub = await js.pull_subscribe(
            "wh.dlq", durable=f"dlq-inspect-{time.monotonic_ns()}", stream=DLQ_STREAM
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not dlq_payloads:
            try:
                msgs = await sub.fetch(1, timeout=2)
                dlq_payloads = [m.data for m in msgs]
            except Exception:  # noqa: BLE001 - fetch timeout while DLQ not yet written
                await asyncio.sleep(0.5)
    finally:
        await client.drain()

    assert dlq_payloads, "malformed message was never transferred to the DLQ stream"
    text = dlq_payloads[0].decode("utf-8")
    assert '"classification": "PERMANENT"' in text or '"classification":"PERMANENT"' in text
    assert "power_history" in text

    # The malformed source message is ACKed (not redelivered forever).
    info, _ = await _consumer_info(stack, env)
    assert info.num_pending == 0 and info.num_ack_pending == 0

    _terminate(writer, reader)


async def test_reliable_durable_consumer_survives_process_restart(
    env: _Env, stack: R5Stack, processes
) -> None:
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")
    await _wait_delivery(env, env.reader_port, "power_history")

    await _jetstream_publish(stack.nats, _reliable_sample("restart-1"))
    first = await _kafka_drain(stack, min_messages=1)
    assert any(b'"sample_id":"restart-1"' in body for body in first)

    info_before, durable_name = await _consumer_info(stack, env)
    assert info_before.num_pending == 0

    # Restart only the reliable process; the Writer and committed generations
    # stay in place. The durable consumer must rebind by its stable name.
    reader.send_signal(signal.SIGTERM)
    assert reader.wait(timeout=15) == 0
    restarted_reader = _spawn_reader(env)
    try:
        await _wait_ready(env, env.reader_port, role="reliable")
        info_after, rebound_name = await _consumer_info(stack, env)
        assert rebound_name == durable_name
        # JetStream still tracks the same durable (stream sequence is retained).
        assert info_after.config.durable_name == durable_name

        # A freshly published message after restart is consumed, proving the
        # rebound consumer actively pulls rather than merely existing.
        await _jetstream_publish(stack.nats, _reliable_sample("restart-2"))
        second = await _kafka_drain(stack, min_messages=1)
        assert any(b'"sample_id":"restart-2"' in body for body in second)
    finally:
        if restarted_reader.poll() is None:
            restarted_reader.send_signal(signal.SIGTERM)
            restarted_reader.wait(timeout=15)


async def _wait_kafka_ready(stack: R5Stack, timeout: float = 60.0) -> None:
    """Wait until a restarted KRaft broker actually bootstraps clients."""
    import aiokafka

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        consumer = aiokafka.AIOKafkaConsumer(
            bootstrap_servers=stack.kafka,
            group_id=f"kafka-ready-{time.monotonic_ns()}",
            request_timeout_ms=3000,
        )
        started = False
        try:
            await asyncio.wait_for(consumer.start(), timeout=5)
            started = True
            return
        except Exception as exc:  # noqa: BLE001 - KRaft still warming up
            last = exc
            await asyncio.sleep(1.0)
        finally:
            if started:
                await consumer.stop()
    pytest.fail(f"Kafka did not become ready after restart: {last}")


async def _dlq_payloads(nats_url: str, window_seconds: float = 3.0) -> list[bytes]:
    import nats

    client = await nats.connect(servers=nats_url, connect_timeout=5)
    found: list[bytes] = []
    try:
        js = client.jetstream()
        sub = await js.pull_subscribe(
            "wh.dlq", durable=f"dlq-inspect-{time.monotonic_ns()}", stream=DLQ_STREAM
        )
        deadline = time.monotonic() + window_seconds
        while time.monotonic() < deadline:
            try:
                messages = await sub.fetch(10, timeout=2)
            except Exception:  # noqa: BLE001 - empty DLQ ends the inspection window
                break
            found.extend(message.data for message in messages)
    finally:
        await client.drain()
    return found


async def test_reliable_kafka_outage_redelivers_without_dlq(
    env: _Env, stack: R5Stack, processes
) -> None:
    """A transient Kafka outage must redeliver the unacked message, never DLQ it."""
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")
    await _wait_delivery(env, env.reader_port, "power_history")

    # Baseline: the healthy path lands and ACKs.
    await _jetstream_publish(stack.nats, _reliable_sample("fault-ok"))
    baseline = await _kafka_drain(stack, min_messages=1)
    assert any(b'"sample_id":"fault-ok"' in body for body in baseline)

    # Stop the real Kafka broker, then offer a message JetStream must retain.
    stack.stop_container("kafka")
    try:
        await _jetstream_publish(stack.nats, _reliable_sample("fault-gap-1"))

        # Stay beyond ack_wait_seconds (30 s): the sink cannot settle the write
        # and must not ACK the source message or send it to the DLQ.
        await asyncio.sleep(35.0)
        info, _ = await _consumer_info(stack, env)
        assert info.num_pending + info.num_ack_pending >= 1
        assert not await _dlq_payloads(stack.nats, window_seconds=2.0)
        assert reader.poll() is None and writer.poll() is None
    finally:
        stack.start_container("kafka")
        await _wait_kafka_ready(stack)

    # After Kafka returns, the retained message is redelivered and eventually
    # explicitly ACKed; the DLQ stays empty because the failure was transient.
    records = await _kafka_drain(stack, min_messages=1, timeout=90.0)
    assert any(b'"sample_id":"fault-gap-1"' in body for body in records), "redelivery lost"

    deadline = time.monotonic() + 30.0
    info = None
    while time.monotonic() < deadline:
        info, _ = await _consumer_info(stack, env)
        if info.num_pending == 0 and info.num_ack_pending == 0:
            break
        await asyncio.sleep(0.5)
    assert info is not None and info.num_pending == 0 and info.num_ack_pending == 0
    assert not await _dlq_payloads(stack.nats, window_seconds=3.0)

    await _wait_delivery(env, env.reader_port, "power_history")
    _terminate(writer, reader)
