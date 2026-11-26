"""真实 CLI Storage、Kafka、TDengine 与 taosdump 的 500 点备份恢复验收。"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import pytest
import yaml
from aiokafka import AIOKafkaProducer
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as storage_pb
from contracts import storage_service_pb2_grpc as storage_rpc
from storage_service.domain.quality_bitmap import unpack_quality_bitmap
from storage_service.domain.wide_schema import WideSchema
from storage_service.infrastructure.adapters.outbound.tdengine_backup import (
    NativeTDengineBackup,
)
from tests.cli.e2e_util import last_json, run_cli
from tests.fixtures.services import r5_stack
from tests.storage_service.integration.test_cli_restart_activation import (
    _create_storage_topic,
    _documents,
    _publish,
    _service_document,
    _wait_status,
    _write_cli_yaml,
)
from tests.support.configuration_live import (
    LiveConfiguration,
    reserve_port,
    write_live_configuration,
)

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "docs/audit/storage_wide_table_d5_round3_20261126_evidence"
TOOLS = Path("/tmp/windhub-d5-r3-tools")
POINT_IDS = tuple(f"p_{index:03d}" for index in range(500))
FARM = "farm-a"
DEVICE = "WTG01"
DEVICE_CONFIG_ID = "device_farm-a_v1.0.0"
PASSWORD = "taosdata"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.kafka,
]


def _timestamp(value: datetime) -> Timestamp:
    result = Timestamp()
    result.FromDatetime(value)
    return result


def _wide_documents(service_document: dict[str, Any]) -> list[Any]:
    documents = _documents(service_document, "1.0.0")
    for document in documents:
        kind = document.config_id.config_type
        if kind not in {"business_points", "point_table"}:
            continue
        body = yaml.safe_load(document.yaml_bytes)
        if kind == "business_points":
            body["business_points"] = {
                point: {"data_type": "float64", "unit": "kilowatt"} for point in POINT_IDS
            }
        else:
            body["points"] = {
                point: {
                    "unit": "kilowatt",
                    "business_point_id": point,
                    "data_type": "float64",
                }
                for point in POINT_IDS
            }
        document.yaml_bytes = yaml.safe_dump(body, sort_keys=False).encode("utf-8")
    return documents


def _expected_value(row: int, point: int) -> tuple[float | None, int]:
    if (point + row) % 37 == 0:
        return None, 0
    return row * 10_000 + point + 0.25, 1 + (point + row) % 3


def _kafka_row(sample_time: datetime, row: int) -> dict[str, Any]:
    points: dict[str, list[Any]] = {}
    for point, point_id in enumerate(POINT_IDS):
        value, quality = _expected_value(row, point)
        points[point_id] = [value, quality]
    return {
        "schema_version": 3,
        "wind_farm_id": FARM,
        "device_config_id": DEVICE_CONFIG_ID,
        "device_id": DEVICE,
        "data_level": "protocol",
        "sample_time": sample_time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "points": points,
    }


async def _query(
    stub: storage_rpc.StorageServiceStub,
    *,
    start: datetime,
    end: datetime,
    metadata: tuple[tuple[str, str], ...],
) -> list[storage_pb.StorageGroup]:
    request = storage_pb.QuerySamplesRequest(
        filter=storage_pb.StorageFilter(
            wind_farm_id=FARM,
            device_ids=[DEVICE],
            data_level=storage_pb.STORAGE_DATA_LEVEL_PROTOCOL,
            start_time=_timestamp(start),
            end_time=_timestamp(end),
        ),
        page_size=10,
    )
    groups: list[storage_pb.StorageGroup] = []
    token = ""
    for _ in range(10):
        response = await stub.QuerySamples(
            storage_pb.QuerySamplesRequest(
                filter=request.filter, page_size=request.page_size, page_token=token
            ),
            metadata=metadata,
            timeout=30,
        )
        groups.extend(response.groups)
        if response.complete:
            assert not response.next_page_token
            return groups
        assert response.next_page_token and response.next_page_token != token
        token = response.next_page_token
    raise AssertionError("QuerySamples pagination did not converge")


async def _wait_ingested(
    stub: storage_rpc.StorageServiceStub,
    *,
    start: datetime,
    end: datetime,
    metadata: tuple[tuple[str, str], ...],
) -> list[storage_pb.StorageGroup]:
    deadline = time.monotonic() + 120
    latest: Any = None
    while time.monotonic() < deadline:
        try:
            groups = await _query(stub, start=start, end=end, metadata=metadata)
            latest = [(group.device_id, len(group.rows)) for group in groups]
            if len(groups) == 1 and len(groups[0].rows) == 2:
                return groups
        except grpc.aio.AioRpcError as exc:
            latest = exc
        await asyncio.sleep(0.5)
    raise AssertionError(f"two 500-point rows were not queryable: {latest}")


def _assert_rpc_rows(groups: list[storage_pb.StorageGroup]) -> None:
    assert len(groups) == 1
    group = groups[0]
    assert [column.point_id for column in group.columns] == list(POINT_IDS)
    assert len(group.rows) == 2
    for row_index, row in enumerate(group.rows):
        assert len(row.values) == 500
        qualities = unpack_quality_bitmap(row.quality, 500)
        for point, value in enumerate(row.values):
            expected, quality = _expected_value(row_index, point)
            assert qualities[point] == quality
            if expected is None:
                assert value.WhichOneof("value") is None
            else:
                assert value.WhichOneof("value") == "value_f64"
                assert value.value_f64 == expected


def _proc_tty(pid: int) -> dict[str, Any]:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = stat[stat.rfind(")") + 2 :].split()
    return {
        "pid": pid,
        "tty_nr": int(fields[4]),
        "stdin": os.readlink(f"/proc/{pid}/fd/0"),
        "argv": Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0"),
    }


async def _observe_taosdump(artifact_root: Path, stop: asyncio.Event) -> list[dict[str, Any]]:
    """直接读取真实子进程内核状态；slave isatty 不能证明 controlling TTY。"""
    observations: dict[int, dict[str, Any]] = {}
    needle = os.fsencode(str(artifact_root))
    while not stop.is_set():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                if b"taosdump" not in cmdline or needle not in cmdline:
                    continue
                pid = int(entry.name)
                observations[pid] = _proc_tty(pid)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        await asyncio.sleep(0.002)
    result = []
    for observation in observations.values():
        result.append(
            {
                **observation,
                "argv": [os.fsdecode(part) for part in observation["argv"] if part],
                "controlling_tty": observation["tty_nr"] != 0,
            }
        )
    return result


def _run_restore(command: list[str], env: dict[str, str], output_path: Path) -> None:
    """用 forkpty 给真实恢复进程独立 controlling TTY，密码只写 PTY。"""
    pid, master = pty.fork()
    if pid == 0:
        os.execve(command[0], command, env)
    output = bytearray()
    status = 0
    deadline = time.monotonic() + 180
    try:
        attributes = termios.tcgetattr(master)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(master, termios.TCSANOW, attributes)
        os.write(master, (PASSWORD + "\n").encode("utf-8"))
        while True:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 4 * 1024 * 1024:
                    raise AssertionError("taosdump restore output exceeded evidence budget")
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid and not ready:
                break
            if time.monotonic() >= deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise AssertionError("taosdump restore timed out")
        if status == 0:
            _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)
        output_path.write_bytes(output)
    assert PASSWORD.encode("utf-8") not in output
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (
        status,
        output[-4000:].decode("utf-8", errors="replace"),
    )


def _verify_restored(endpoint: str, database: str, table: str) -> None:
    taosws = pytest.importorskip("taosws", reason="NOT_RUN: real TDengine SQL driver unavailable")
    host, port = endpoint.rsplit(":", 1)
    connection = taosws.connect(
        user="root", password=PASSWORD, host=host, port=int(port), read_timeout="30"
    )
    try:
        rows = list(connection.query(f"SELECT * FROM `{database}`.`{table}` ORDER BY ts"))
        assert len(rows) == 2
        for row_index, row in enumerate(rows):
            assert len(row) == 502
            for point in range(500):
                expected, _quality = _expected_value(row_index, point)
                assert row[point + 1] == expected
            qualities = unpack_quality_bitmap(bytes(row[-1]), 500)
            assert qualities == tuple(_expected_value(row_index, point)[1] for point in range(500))
    finally:
        connection.close()


def _assert_no_password_in_tree(root: Path) -> None:
    needle = PASSWORD.encode("utf-8")
    for path in root.rglob("*"):
        if path.is_file():
            assert needle not in path.read_bytes(), f"password leaked into {path}"


def _real_pty_probe(tmp_path: Path) -> dict[str, Any]:
    """在真实独立会话子进程中观测 /dev/tty 和内核 tty_nr。"""
    script = r"""
import json, os, pathlib, sys
result = {"stdin_isatty": os.isatty(0), "stdin_name": os.ttyname(0)}
stat = pathlib.Path('/proc/self/stat').read_text()
result['tty_nr'] = int(stat[stat.rfind(')') + 2:].split()[4])
try:
    fd = os.open('/dev/tty', os.O_RDWR)
except OSError as exc:
    result['dev_tty_errno'] = exc.errno
else:
    result['dev_tty_name'] = os.ttyname(fd)
    os.close(fd)
pathlib.Path(sys.argv[1]).write_text(json.dumps(result))
"""
    output = tmp_path / "pty-probe.json"
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(output)],
            stdin=slave,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        os.close(slave)
    try:
        assert process.wait(timeout=10) == 0
    finally:
        os.close(master)
    result = json.loads(output.read_text(encoding="utf-8"))
    result["controlling_tty"] = result["tty_nr"] != 0 and "dev_tty_name" in result
    return result


@pytest.mark.asyncio
async def test_real_backup_and_taosdump_rename_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable")
    pytest.importorskip("taosws", reason="NOT_RUN: real TDengine SQL driver unavailable")
    assert (TOOLS / "taosdump").is_file() and (TOOLS / "libtaos.so").is_file()
    probe = _real_pty_probe(tmp_path)
    print(f"PTY_PROBE={json.dumps(probe, sort_keys=True)}", flush=True)
    stack = await asyncio.to_thread(r5_stack.up_stack)
    token = uuid4().hex[:10]
    storage_port = reserve_port()
    operator_token = f"backup-storage-operator-{token}-0123456789abcdef"
    cursor_secret = f"backup-storage-cursor-{token}-0123456789abcdef"
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", operator_token)
    live: LiveConfiguration | None = None
    cli_yaml = tmp_path / "cli" / "cli.yaml"
    cache_root = tmp_path / "storage-cache"
    bootstrap = tmp_path / "storage-bootstrap.yaml"
    channel: grpc.aio.Channel | None = None
    storage_log = tmp_path / "logs/storage.log"
    try:
        root = tmp_path / "configuration"
        root.mkdir(parents=True)
        live = write_live_configuration(root, reserve_port())
        viewer_token = live.tokens["viewer"]
        started = await asyncio.to_thread(
            live.cli, "service", "start", "configuration", "--yes", check=False
        )
        assert started["exit_code"] == 0, started

        service_document = _service_document(
            stack=stack, storage_port=storage_port, token=token, work_root=tmp_path
        )
        service_document["service"]["backup"]["enabled"] = True
        service_document["service"]["backup"]["timeout_s"] = 30
        service_document["service"]["tdengine"]["ingestion"]["batch_wait_ms"] = 100
        await _publish(live, _wide_documents(service_document))
        topic = service_document["service"]["kafka"]["topic"]
        await _create_storage_topic(stack.kafka, topic)
        bootstrap.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "file_version": "1.0.0",
                    "service_id": "storage-main",
                    "deployment_id": "prod-a",
                    "configuration": {
                        "endpoint": f"127.0.0.1:{live.port}",
                        "token_env": "WIND_HUB_CONFIG_VIEWER_TOKEN",
                    },
                    "nats": {"servers": [stack.nats]},
                    "cache": {"directory": str(cache_root)},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        process_env = {
            "PATH": f"{TOOLS}:{os.environ['PATH']}",
            "LD_LIBRARY_PATH": f"{TOOLS}:{os.environ.get('LD_LIBRARY_PATH', '')}",
            "WIND_HUB_CONFIG_VIEWER_TOKEN": viewer_token,
            "WIND_HUB_STORAGE_OPERATOR_TOKEN": operator_token,
            "WIND_HUB_TDENGINE_USER": "root",
            "WIND_HUB_TDENGINE_PASSWORD": PASSWORD,
            "WIND_HUB_STORAGE_CURSOR_SECRET": cursor_secret,
            "WIND_HUB_TEST_NATS_URL": stack.nats,
        }
        _write_cli_yaml(
            cli_yaml,
            storage_port=storage_port,
            bootstrap=bootstrap,
            work_root=tmp_path,
            env=process_env,
        )
        start = await asyncio.to_thread(run_cli, cli_yaml, "service", "start", "storage", "--yes")
        payload = last_json(start.stdout)
        assert start.returncode == 0, start.stderr
        assert payload["status"] == "SUCCESS", payload
        assert payload["data"]["business"]["health"] == "HEALTHY"

        channel = grpc.aio.insecure_channel(f"127.0.0.1:{storage_port}")
        await _wait_status(
            channel,
            operator_token,
            lambda status: status.health == "HEALTHY"
            and status.target_revision == status.active_revision
            and status.dependencies[0].detail == "READY",
            "Storage READY",
        )
        metadata = (("authorization", f"Bearer {operator_token}"),)
        stub = storage_rpc.StorageServiceStub(channel)
        base = datetime.now(UTC).replace(microsecond=0)
        producer = AIOKafkaProducer(
            bootstrap_servers=stack.kafka, acks="all", enable_idempotence=True, linger_ms=0
        )
        await producer.start()
        try:
            for row in range(2):
                body = _kafka_row(base + timedelta(seconds=row), row)
                await producer.send_and_wait(
                    topic,
                    key=f"{FARM}:{DEVICE}".encode(),
                    value=json.dumps(body, separators=(",", ":")).encode(),
                )
        finally:
            await producer.stop()
        groups = await _wait_ingested(
            stub,
            start=base - timedelta(seconds=1),
            end=base + timedelta(seconds=3),
            metadata=metadata,
        )
        _assert_rpc_rows(groups)
        print("SOURCE_QUERY_SAMPLES=2 rows x 500 values, values and quality matched", flush=True)

        backup_root = tmp_path / "backups"
        stop_observation = asyncio.Event()
        observer = asyncio.create_task(_observe_taosdump(backup_root, stop_observation))
        task = await stub.CreateBackup(
            storage_pb.CreateBackupRequest(request_id=f"backup-{token}", wind_farm_id=FARM),
            metadata=metadata,
            timeout=30,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if task.state in {
                storage_pb.STORAGE_TASK_STATE_SUCCEEDED,
                storage_pb.STORAGE_TASK_STATE_FAILED,
                storage_pb.STORAGE_TASK_STATE_INTERRUPTED,
            }:
                break
            await asyncio.sleep(0.1)
            task = await stub.GetBackupTask(
                storage_pb.GetStorageTaskRequest(task_id=task.task_id),
                metadata=metadata,
                timeout=30,
            )
        stop_observation.set()
        observations = await observer
        print(
            f"TAOSDUMP_PROCESS_OBSERVATIONS={json.dumps(observations, sort_keys=True)}", flush=True
        )
        print(
            f"BACKUP_TASK_STATE={storage_pb.StorageTaskState.Name(task.state)} "
            f"error_code={task.error_code!r} message={task.message!r}",
            flush=True,
        )
        for observation in observations:
            assert PASSWORD not in " ".join(observation["argv"])
        assert PASSWORD not in task.message and PASSWORD not in task.error_code
        assert PASSWORD.encode("utf-8") not in storage_log.read_bytes()
        for candidate in backup_root.rglob("*"):
            if candidate.is_file():
                assert PASSWORD.encode("utf-8") not in candidate.read_bytes()
        print("PASSWORD_LEAK_SCAN=argv, task errors, Storage log and artifact clean", flush=True)
        assert task.state == storage_pb.STORAGE_TASK_STATE_SUCCEEDED, task
        assert task.integrity_verified and task.artifact_id == task.task_id
        artifact = Path(task.artifact_path)
        assert artifact.is_dir() and task.size_bytes > 0 and len(task.sha256) == 64
        database = WideSchema.database_name(FARM)
        verified_size, verified_digest = NativeTDengineBackup.verify_manifest(
            artifact, database=database
        )
        assert (verified_size, verified_digest) == (task.size_bytes, task.sha256)
        manifest = json.loads((artifact / "storage_manifest.json").read_text(encoding="utf-8"))
        assert manifest["historical_configuration"]["documents"]
        assert manifest["historical_configuration"]["device_bindings"]
        assert manifest["historical_configuration"]["semantic_pins"]
        assert manifest["wide_schema_ownership"]
        assert (
            hashlib.sha256(
                json.dumps(
                    {key: value for key, value in manifest.items() if key != "sha256"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            == task.sha256
        )
        print(
            f"BACKUP_VERIFIED path={artifact} size={task.size_bytes} sha256={task.sha256} "
            f"history_documents={len(manifest['historical_configuration']['documents'])} "
            f"wide_ownership={len(manifest['wide_schema_ownership'])}",
            flush=True,
        )

        restored_database = f"backup_restore_{token}"
        restore_command = [
            str(TOOLS / "taosdump"),
            "-Z",
            "1",
            "-h",
            stack.tdengine_rest.rsplit(":", 1)[0],
            "-P",
            stack.tdengine_rest.rsplit(":", 1)[1],
            "-u",
            "root",
            "-p",
            "-i",
            str(artifact),
            "-W",
            f"{database}={restored_database}",
        ]
        restore_env = {
            **os.environ,
            "PATH": process_env["PATH"],
            "LD_LIBRARY_PATH": process_env["LD_LIBRARY_PATH"],
        }
        restore_output = tmp_path / "taosdump-restore.log"
        await asyncio.to_thread(_run_restore, restore_command, restore_env, restore_output)
        table = WideSchema.subtable_name(DEVICE)
        await asyncio.to_thread(_verify_restored, stack.tdengine_rest, restored_database, table)
        print(
            f"RESTORE_VERIFIED database={restored_database} table={table} "
            "2 rows x 500 values and quality matched",
            flush=True,
        )

        assert PASSWORD not in " ".join(restore_command)
        assert PASSWORD.encode("utf-8") not in storage_log.read_bytes()
        _assert_no_password_in_tree(artifact)
        print("RESTORE_PASSWORD_LEAK_SCAN=restore argv, Storage log and artifact clean", flush=True)
    finally:
        if channel is not None:
            await channel.close()
        stop = await asyncio.to_thread(run_cli, cli_yaml, "service", "stop", "storage", "--yes")
        if cli_yaml.exists():
            assert stop.returncode == 0, (stop.stdout, stop.stderr)
        if live is not None:
            stopped = await asyncio.to_thread(
                live.cli, "service", "stop", "configuration", "--yes", check=False
            )
            assert stopped["exit_code"] == 0, stopped
        if storage_log.exists():
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            shutil.copy2(storage_log, EVIDENCE / "backup_pty_real_storage.log")
            print(f"STORAGE_LOG={EVIDENCE / 'backup_pty_real_storage.log'}", flush=True)
        await asyncio.to_thread(r5_stack.down_stack, stack)
