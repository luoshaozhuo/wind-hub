"""Gate-5 throughput/stability test for the realtime NATS -> Redis path.

Topology under test mirrors the acceptance target (design v1.17 section 14):
N realtime Deliveries, each backed by its own Redis Sink connection, each
holding 500 device keys so one 100 ms scheduler cycle emits exactly one
500-sample write_batch. A real NATS Core publisher drives the load; every
component is real (NATS broker, Redis, two sink OS processes over mTLS) -- only
the load generator is test code.

Scale and duration are environment-controlled so the same test can run as a
short calibration smoke or as the full 20-sink / 10-minute stability run:

    SINK_PERF_SINKS     number of sinks+deliveries      (default 20)
    SINK_PERF_HZ        scheduler ticks per second      (default 10)
    SINK_PERF_POINTS    device keys per sink = batch    (default 500)
    SINK_PERF_SECONDS   steady-state duration           (default 60)

Acceptance target: >=200 batches/s and >=100000 values/s sustained, zero failed
write attempts. The hard rate assertion is only applied at the full acceptance
scale (20x10x500, >=600 s); smaller runs record measured numbers without
fabricating a pass.
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
from tests.fixtures.services.r5_stack import R5Stack, docker_available
from tests.sink_service._stage1_fixtures import (
    REALTIME_SERVICE,
    RELIABLE_SERVICE,
    snapshot_docs,
    template_path,
    write_tree,
)
from tests.sink_service._stage1_tls import write_mtls_bundle

pytestmark = [pytest.mark.performance, pytest.mark.system]

REPO_ROOT = Path(__file__).resolve().parents[2]
_CALLERS = [
    {
        "caller_id": "maintenance",
        "certificate_identity": "spiffe://wind-hub/maintenance",
        "permissions": ["query", "control", "config"],
    },
]

SINKS = int(os.environ.get("SINK_PERF_SINKS", "20"))
HZ = int(os.environ.get("SINK_PERF_HZ", "10"))
POINTS = int(os.environ.get("SINK_PERF_POINTS", "500"))
SECONDS = float(os.environ.get("SINK_PERF_SECONDS", "60"))
POLICY = os.environ.get("SINK_PERF_POLICY", "change_with_heartbeat")
if POLICY not in {"periodic", "on_change", "change_with_heartbeat"}:
    raise RuntimeError(f"unsupported SINK_PERF_POLICY: {POLICY}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _proc_rss_kib(pid: int) -> int:
    """VmRSS (KiB) of an OS process, read from procfs."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        pass
    return -1


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
        pytest.skip("Gate-5 runs against an external pinned stack (SINK_R5_*)")
        raise  # pragma: no cover


@pytest.fixture
def stack() -> R5Stack:
    return _stack()


@pytest.fixture
def env(tmp_path: Path, stack: R5Stack) -> _Env:
    writer_port, reader_port = _free_port(), _free_port()
    bundle = write_mtls_bundle(tmp_path)
    value = _Env(tmp_path, bundle, writer_port, reader_port)
    redis_port = int(stack.redis.rsplit(":", 1)[1])

    sinks = {
        f"redis_{idx:02d}": {
            "template_id": "redis_realtime",
            "template_version": "1.0.0",
            "connection": {"host": "127.0.0.1", "port": redis_port},
            "overrides": {},
        }
        for idx in range(SINKS)
    }
    # 500 distinct device keys per delivery -> one 500-sample batch per cycle.
    deliveries = {
        f"power_{idx:02d}": {
            "template_id": "realtime_power",
            "template_version": "1.0.0",
            "sink_id": f"redis_{idx:02d}",
            "desired_state": "START",
            "source": {
                "wind_farm_id": "farm-a",
                "collector_id": "collector-1",
                "devices": [f"WT{idx:03d}D{point:03d}" for point in range(POINTS)],
                "points": ["active_power"],
            },
            "overrides": {},
        }
        for idx in range(SINKS)
    }
    docs = snapshot_docs(
        writer_port=writer_port,
        reader_port=reader_port,
        config_root=str(tmp_path),
        callers=_CALLERS,
        include_templates=("redis_realtime", "realtime_power"),
        sinks=sinks,
        deliveries=deliveries,
    )
    docs[template_path("realtime_power")]["output"]["policy"] = POLICY
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
    return subprocess.Popen(
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
                proc.wait(timeout=20)
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


async def _wait_ready(env: _Env, port: int, role: str) -> None:
    deadline = time.monotonic() + 40.0
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
            last = RuntimeError(f"role {info.process_role!r}, want {role!r}")
        except Exception as exc:  # noqa: BLE001
            last = exc
        await asyncio.sleep(0.3)
    pytest.fail(f"process on {port} never ready: {last}")


async def _wait_all_running(env: _Env, port: int) -> None:
    """All N deliveries must settle RUNNING before load is applied."""
    deadline = time.monotonic() + 60.0
    last: dict[str, str] = {}
    while time.monotonic() < deadline:
        async with _channel(env, port) as channel:
            status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
                common_pb2.Empty()
            )
        last = {item.name: item.health for item in status.dependencies}
        if len(last) >= SINKS and all(health == "RUNNING" for health in last.values()):
            return
        if "FAILED" in last.values():
            detail = {item.name: item.detail for item in status.dependencies}
            pytest.fail(f"delivery FAILED before load: {detail}")
        await asyncio.sleep(0.5)
    pytest.fail(f"deliveries not all RUNNING: {last}")


async def _metrics(env: _Env, port: int) -> dict[str, float]:
    async with _channel(env, port) as channel:
        response = await common_pb2_grpc.CommonServiceStub(channel).GetMetrics(
            common_pb2.GetMetricsRequest(window_ms=60000)
        )
    return {item.name.rsplit(".", 1)[-1]: item.value for item in response.metrics}


async def _publish_load(stack: R5Stack, stop: asyncio.Event) -> tuple[int, int, int, int]:
    """Drive N sinks at HZ ticks/s, POINTS samples/tick, smoothly paced.

    A real collector spreads its samples across the 100 ms window; bursting all
    SINKS*POINTS messages at the tick boundary would manufacture unconfirmed-
    overwrite drops inside the latest-value cache. Each tick is split into 20
    equally spaced micro-bursts instead.

    Return (sent, errors, late_slots, planned_slots) so the caller can tell a
    sink throughput failure from a load generator that cannot keep the pace.
    """
    import nats

    client = await nats.connect(servers=stack.nats, connect_timeout=10)
    sent = 0
    errors = 0
    late_slots = 0
    planned_slots = 0
    period = 1.0 / HZ
    chunks = 20
    per_chunk = max(1, -(-(SINKS * POINTS) // chunks))  # ceil, index guard trims the tail
    slot = period / chunks
    next_tick = time.monotonic()
    tick = 0
    try:
        while not stop.is_set():
            tick_started = next_tick
            for chunk_idx in range(chunks):
                if stop.is_set():
                    break
                planned_slots += 1
                now = datetime.now(UTC)
                published = 0
                for offset in range(per_chunk):
                    index = chunk_idx * per_chunk + offset
                    if index >= SINKS * POINTS:
                        break
                    sink_idx, point_idx = divmod(index, POINTS)
                    sample = SampleMessage(
                        wind_farm_id="farm-a",
                        collector_id="collector-1",
                        task_id=f"task-{sink_idx}",
                        device_id=f"WT{sink_idx:03d}D{point_idx:03d}",
                        point_id="active_power",
                        sample_id=f"s-{tick}-{sink_idx}-{point_idx}",
                        # Periodic writes every tick regardless of value; the
                        # change policies need a per-tick value beyond the
                        # absolute 1.0 threshold to keep triggering writes.
                        value=(
                            float(point_idx) if POLICY == "periodic" else float(point_idx + tick)
                        ),
                        quality="good",
                        sample_time=now,
                        publish_time=now,
                        max_staleness_ms=60000,
                    )
                    try:
                        await client.publish(sample.subject("realtime"), sample.encode())
                        published += 1
                    except Exception:  # noqa: BLE001 - counted, never silently hidden
                        errors += 1
                await client.flush()
                sent += published
                target_time = tick_started + slot * (chunk_idx + 1)
                wait = target_time - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                elif time.monotonic() - target_time > slot:
                    late_slots += 1
            tick += 1
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Generator fell behind a full tick; resync instead of bursting.
                next_tick = time.monotonic()
    finally:
        await client.drain()
    return sent, errors, late_slots, planned_slots


def _redis_stat(stack: R5Stack, *command: str) -> str:
    result = stack.docker("exec", stack.container("redis"), "redis-cli", *command, timeout=30.0)
    if result.returncode != 0:
        raise RuntimeError(f"redis-cli {' '.join(command)} failed: {result.stderr.strip()}")
    return result.stdout


def _redis_eval_calls(stack: R5Stack) -> int:
    """Independent EVAL counter: one EVAL == one <=500-sample write_batch.

    ``total_commands_processed`` cannot be used because every redis.call inside
    the Lua CAS script increments it; commandstats counts script invocations.
    """
    for line in _redis_stat(stack, "INFO", "commandstats").splitlines():
        if line.startswith("cmdstat_eval:"):
            for field in line.split(":")[1].split(","):
                if field.startswith("calls="):
                    return int(field.split("=", 1)[1])
    raise RuntimeError("cmdstat_eval missing from Redis INFO commandstats")


def _redis_dbsize(stack: R5Stack) -> int:
    return int(_redis_stat(stack, "DBSIZE").strip().splitlines()[-1])


def test_realtime_throughput_meets_gate5_target(env: _Env, stack: R5Stack, processes) -> None:
    writer, _reader = processes
    asyncio.run(_wait_ready(env, env.writer_port, "realtime"))
    asyncio.run(_wait_ready(env, env.reader_port, "reliable"))
    asyncio.run(_wait_all_running(env, env.writer_port))

    rss_before = _proc_rss_kib(writer.pid)
    stop = asyncio.Event()

    async def scenario() -> dict[str, float]:
        before = await _metrics(env, env.writer_port)
        eval_before = await asyncio.to_thread(_redis_eval_calls, stack)
        probe_stop = asyncio.Event()
        max_dbsize = 0

        async def dbsize_probe() -> None:
            # Keys carry a 3 s PXAT (engine max_age_ms); a post-run DBSIZE is
            # always zero, so sample the live keyspace while load is flowing.
            nonlocal max_dbsize
            while not probe_stop.is_set():
                max_dbsize = max(max_dbsize, await asyncio.to_thread(_redis_dbsize, stack))
                await asyncio.sleep(1.0)

        started = time.monotonic()
        probe_task = asyncio.create_task(dbsize_probe())
        load_task = asyncio.create_task(_publish_load(stack, stop))
        await asyncio.sleep(SECONDS)
        stop.set()
        sent, publish_errors, late_slots, planned_slots = await load_task
        elapsed = time.monotonic() - started
        probe_stop.set()
        await probe_task
        # Drain window so in-flight batches settle into the 1 s metric snapshots.
        await asyncio.sleep(4.0)
        after = await _metrics(env, env.writer_port)
        eval_after = await asyncio.to_thread(_redis_eval_calls, stack)
        return {
            "elapsed": elapsed,
            "sent": float(sent),
            "publish_errors": float(publish_errors),
            "late_slots": float(late_slots),
            "planned_slots": float(planned_slots),
            "delivered_before": before.get("delivered_total", 0.0),
            "delivered_after": after.get("delivered_total", 0.0),
            "received_after": after.get("received_total", 0.0),
            "dead_letter_after": after.get("dead_letter_total", 0.0),
            # Each write_batch is exactly one Redis EVAL invocation (one EVAL
            # carries up to 500 CAS keys); cmdstat_eval counts invocations only.
            "redis_eval_batches": float(eval_after - eval_before),
            "redis_max_dbsize": float(max_dbsize),
            "rate_per_second": after.get("delivered_rate_per_second", 0.0),
            "inflight": after.get("io_inflight", 0.0),
            "failed": after.get("failed_total", 0.0),
            "dropped": after.get("dropped_total", 0.0),
            "p99_ms": after.get("write_latency_ms_p99", 0.0),
        }

    result = asyncio.run(scenario())
    rss_after = _proc_rss_kib(writer.pid)

    delivered = result["delivered_after"] - result["delivered_before"]
    batches = result["redis_eval_batches"]
    elapsed = result["elapsed"]
    values_per_second = delivered / elapsed
    batches_per_second = batches / elapsed
    target_send_rate = SINKS * HZ * POINTS
    generator_rate = result["sent"] / elapsed
    late_ratio = result["late_slots"] / max(1.0, result["planned_slots"])
    expected_keys = SINKS * POINTS

    print(
        "\n"
        f"GATE5 sinks={SINKS} hz={HZ} points/batch={POINTS} policy={POLICY} "
        f"duration={elapsed:.0f}s\n"
        f"  samples_sent={result['sent']:.0f} generator_rate={generator_rate:.1f}/s "
        f"(target send {target_send_rate}/s, late slots {late_ratio:.2%})\n"
        f"  engine received_total={result['received_after']:.0f} "
        f"dead_letter_total={result['dead_letter_after']:.0f}\n"
        f"  publish_errors={result['publish_errors']:.0f}\n"
        f"  delivered_delta={delivered:.0f} redis_EVAL_batches={batches:.0f} "
        f"failed={result['failed']:.0f} dropped={result['dropped']:.0f}\n"
        f"  measured values/s={values_per_second:.1f} (target >=100000)\n"
        f"  measured batches/s={batches_per_second:.1f} "
        "(target >=200; one EVAL = one <=500-sample write_batch)\n"
        f"  engine window rate={result['rate_per_second']:.1f} values/s "
        f"p99_write={result['p99_ms']:.2f} ms inflight={result['inflight']:.0f}\n"
        f"  redis max DBSIZE={result['redis_max_dbsize']:.0f} "
        f"(expected {expected_keys} live keys while flowing)\n"
        f"  writer VmRSS={rss_before}->{rss_after} KiB"
    )

    # No failed write attempts and no broker-side publish errors under load.
    assert result["failed"] == 0.0, f"failed write attempts under load: {result['failed']}"
    assert result["publish_errors"] == 0.0, "NATS publish errors under load"
    # The hard rate assertion is only applied at the full acceptance scale,
    # never weakened for a calibration smoke run.
    full_scale = SINKS >= 20 and HZ >= 10 and POINTS >= 500 and SECONDS >= 600
    if full_scale:
        # If the test-side generator itself cannot sustain the target pace, the
        # sink's target-rate capacity stays unproven: record NOT_RUN, never pass.
        if generator_rate < 0.95 * target_send_rate or late_ratio > 0.10:
            pytest.skip(
                f"load generator capped at {generator_rate:.0f}/s "
                f"(late slots {late_ratio:.2%}); sink target-rate NOT_RUN"
            )
        assert values_per_second >= 100000.0, values_per_second
        assert batches_per_second >= 200.0, batches_per_second
