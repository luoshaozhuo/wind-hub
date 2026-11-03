"""Real dual-process Writer/Reader control plane over mTLS.

Two OS processes (``sink_service.main``) share one configuration root:

- the realtime process is the unique Writer;
- the reliable process is the Reader and polls committed generations;
- every call uses real protobuf stubs and mutual TLS;
- the data plane is the stage-one fail-closed factory, so a START cannot be
  claimed successful and that failure must stay honestly observable.

The test exercises the published 13-RPC surface, the section 9.3.1 status
contract, per-process convergence adjudication and graceful SIGTERM shutdown.
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
from pathlib import Path

import grpc
import pytest
import yaml

from contracts import common_pb2, common_pb2_grpc, sink_service_pb2, sink_service_pb2_grpc
from sink_service.application.config import validate_snapshot
from sink_service.domain.config import SnapshotFile
from tests.sink_service._stage1_fixtures import snapshot_docs, write_tree
from tests.sink_service._stage1_tls import write_mtls_bundle

pytestmark = [pytest.mark.system, pytest.mark.contract, pytest.mark.reliability]

REPO_ROOT = Path(__file__).resolve().parents[2]

_CALLERS = [
    {
        "caller_id": "maintenance",
        "certificate_identity": "spiffe://wind-hub/maintenance",
        "permissions": ["query", "control", "config"],
    },
    {
        "caller_id": "readonly",
        "certificate_identity": "spiffe://wind-hub/readonly",
        "permissions": ["query"],
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


def _docs(env: _Env, desired: dict[str, str]) -> dict:
    docs = snapshot_docs(
        writer_port=env.writer_port,
        reader_port=env.reader_port,
        config_root=str(env.root),
        callers=_CALLERS,
    )
    for name in ("sink-realtime", "sink-reliable"):
        docs[f"services/{name}.yaml"]["security"].update(
            ca_cert=str(env.bundle["ca"]),
            server_cert=str(env.bundle["server_crt"]),
            server_key=str(env.bundle["server_key"]),
        )
    docs["services/sink-reliable.yaml"]["config_sync"]["writer_endpoint"] = (
        f"127.0.0.1:{env.writer_port}"
    )
    for delivery_id, state in desired.items():
        docs["instances/deliveries.yaml"]["deliveries"][delivery_id]["desired_state"] = state
    return docs


@pytest.fixture
def env(tmp_path: Path) -> _Env:
    writer_port = _free_port()
    reader_port = _free_port()
    bundle = write_mtls_bundle(tmp_path)
    value = _Env(tmp_path, bundle, writer_port, reader_port)
    docs = _docs(value, {"ems_power": "STOP", "power_history": "STOP"})
    write_tree(tmp_path, docs)
    validate_snapshot(
        tuple(
            SnapshotFile(str(path.relative_to(tmp_path)), path.read_bytes())
            for path in sorted(tmp_path.rglob("*.yaml"))
        )
    )
    return value


@pytest.fixture
def processes(env: _Env):
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    base = [
        sys.executable,
        "-m",
        "sink_service.main",
        "--config-root",
        str(env.root),
    ]
    writer = subprocess.Popen(
        [*base, "--service-file", "services/sink-realtime.yaml"],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    reader = subprocess.Popen(
        [*base, "--service-file", "services/sink-reliable.yaml"],
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


def _channel(env: _Env, port: int, *, client: str | None = None) -> grpc.aio.Channel:
    if client is None:
        credentials = grpc.ssl_channel_credentials(root_certificates=env.bundle["ca"].read_bytes())
    else:
        credentials = grpc.ssl_channel_credentials(
            root_certificates=env.bundle["ca"].read_bytes(),
            private_key=env.bundle[f"{client}_key"].read_bytes(),
            certificate_chain=env.bundle[f"{client}_crt"].read_bytes(),
        )
    return grpc.aio.secure_channel(
        f"127.0.0.1:{port}",
        credentials,
        options=(("grpc.ssl_target_name_override", "localhost"),),
    )


async def _wait_ready(env: _Env, port: int, *, role: str) -> None:
    deadline = time.monotonic() + 20.0
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            async with _channel(env, port, client="admin") as channel:
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
        await asyncio.sleep(0.2)
    pytest.fail(f"process on {port} never became ready: {last}")


def _wire_files(docs: dict) -> list[common_pb2.SnapshotFile]:
    import hashlib

    files = []
    for path, doc in sorted(docs.items()):
        content = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode()
        files.append(
            common_pb2.SnapshotFile(
                relative_path=path,
                content=content,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return files


async def _runtime_revision(channel: grpc.aio.Channel) -> str:
    response = await common_pb2_grpc.CommonServiceStub(channel).GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_RUNTIME)
    )
    return response.revision


async def _wait_runtime(env: _Env, port: int, revision: str) -> None:
    deadline = time.monotonic() + 15.0
    async with _channel(env, port, client="admin") as channel:
        while time.monotonic() < deadline:
            if await _runtime_revision(channel) == revision:
                return
            await asyncio.sleep(0.1)
        current = await _runtime_revision(channel)
    pytest.fail(f"runtime revision stuck at {current}, expected {revision}")


async def test_dual_process_control_plane_contracts(env: _Env, processes) -> None:
    writer, reader = processes
    await _wait_ready(env, env.writer_port, role="realtime")
    await _wait_ready(env, env.reader_port, role="reliable")

    async with (
        _channel(env, env.writer_port, client="admin") as writer_admin,
        _channel(env, env.reader_port, client="admin") as reader_admin,
        _channel(env, env.writer_port, client="readonly") as writer_ro,
        _channel(env, env.writer_port, client="outsider") as outsider,
        _channel(env, env.writer_port) as anonymous,
    ):
        common_w = common_pb2_grpc.CommonServiceStub(writer_admin)
        common_r = common_pb2_grpc.CommonServiceStub(reader_admin)
        common_ro = common_pb2_grpc.CommonServiceStub(writer_ro)
        sink_w = sink_service_pb2_grpc.SinkServiceStub(writer_admin)

        # --- Identity (RPC 1) -------------------------------------------------
        info_w = await common_w.GetServiceInfo(common_pb2.Empty())
        info_r = await common_r.GetServiceInfo(common_pb2.Empty())
        assert info_w.process_role == "realtime"
        assert info_r.process_role == "reliable"
        assert info_w.service_id == info_r.service_id
        assert info_w.contract_version == "v1"

        # --- Capabilities honestly omit the stage-four data plane (RPC 3) ----
        caps = await common_w.GetCapabilities(common_pb2.Empty())
        assert "ApplyConfigSnapshot" in caps.rpc_names
        assert caps.protocols == []

        # --- UNIMPLEMENTED stage-four surface (RPC 7/8/9) --------------------
        for stub, request in (
            (common_w.GetMetrics, common_pb2.GetMetricsRequest()),
            (common_w.GetEvents, common_pb2.GetEventsRequest()),
            (common_w.Diagnose, common_pb2.DiagnoseRequest()),
        ):
            with pytest.raises(grpc.aio.AioRpcError) as excinfo:
                await stub(request)
            assert excinfo.value.code() == grpc.StatusCode.UNIMPLEMENTED

        # --- mTLS: unregistered identity, no cert, insufficient permission ---
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await common_pb2_grpc.CommonServiceStub(outsider).GetServiceInfo(common_pb2.Empty())
        assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await common_pb2_grpc.CommonServiceStub(anonymous).GetServiceInfo(common_pb2.Empty())
        assert excinfo.value.code() == grpc.StatusCode.UNAVAILABLE

        assert await common_ro.GetCapabilities(common_pb2.Empty())
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await sink_service_pb2_grpc.SinkServiceStub(writer_ro).StartDelivery(
                common_pb2.OperationRequest(id="ems_power")
            )
        assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

        initial = await common_w.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_DISK)
        )
        initial_rev = initial.revision

        # --- Only the Writer mutates configuration (RPC 5) -------------------
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await common_r.PushConfigSnapshot(
                common_pb2.PushConfigSnapshotRequest(
                    files=_wire_files(_docs(env, {"ems_power": "STOP", "power_history": "STOP"})),
                    expected_revision=initial_rev,
                )
            )
        assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED

        # --- Invalid candidate is INVALID_ARGUMENT ---------------------------
        bad_docs = _docs(env, {"ems_power": "STOP", "power_history": "STOP"})
        del bad_docs["services/sink-reliable.yaml"]
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await common_w.PushConfigSnapshot(
                common_pb2.PushConfigSnapshotRequest(
                    files=_wire_files(bad_docs), expected_revision=initial_rev
                )
            )
        assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT

        # --- Stale CAS is ABORTED --------------------------------------------
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await common_w.PushConfigSnapshot(
                common_pb2.PushConfigSnapshotRequest(
                    files=_wire_files(_docs(env, {"ems_power": "STOP", "power_history": "STOP"})),
                    expected_revision="0" * 64,
                )
            )
        assert excinfo.value.code() == grpc.StatusCode.ABORTED

        # --- Candidate scope (RPC 4) and NOT_FOUND on an unknown id ----------
        staged = await common_w.PushConfigSnapshot(
            common_pb2.PushConfigSnapshotRequest(
                files=_wire_files(_docs(env, {"ems_power": "STOP", "power_history": "STOP"})),
                expected_revision=initial_rev,
            )
        )
        assert staged.valid and staged.base_revision == initial_rev
        candidate_view = await common_w.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(
                scope=common_pb2.SNAPSHOT_SCOPE_CANDIDATE,
                candidate_id=staged.candidate_id,
            )
        )
        assert candidate_view.revision == initial_rev
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await common_w.GetConfigSnapshot(
                common_pb2.GetConfigSnapshotRequest(
                    scope=common_pb2.SNAPSHOT_SCOPE_CANDIDATE,
                    candidate_id="missing",
                )
            )
        assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND

        # UNSPECIFIED scope must never be silently treated as a confirmed one.
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await common_w.GetConfigSnapshot(
                common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_UNSPECIFIED)
            )
        assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT

        # --- START cannot be claimed: fail-closed, per-process convergence ----
        start_push = await common_w.PushConfigSnapshot(
            common_pb2.PushConfigSnapshotRequest(
                files=_wire_files(_docs(env, {"ems_power": "START", "power_history": "STOP"})),
                expected_revision=initial_rev,
            )
        )
        start_apply = await common_w.ApplyConfigSnapshot(
            common_pb2.ApplyConfigSnapshotRequest(
                candidate_id=start_push.candidate_id, expected_revision=initial_rev
            )
        )
        start_results = {r.object_id: r.status for r in start_apply.apply_results}
        assert start_results == {"ems_power": "FAILED"}
        assert start_apply.file_commit != initial_rev
        assert start_apply.effective_revision == initial_rev
        failed_generation = start_apply.file_commit

        # The Reader independently converges: its own objects are stopped, so it
        # may adopt a generation the Writer cannot make effective.
        await _wait_runtime(env, env.reader_port, failed_generation)
        assert await _runtime_revision(writer_admin) == initial_rev

        # --- STOP converges in both processes through the generation pointer --
        stop_push = await common_w.PushConfigSnapshot(
            common_pb2.PushConfigSnapshotRequest(
                files=_wire_files(_docs(env, {"ems_power": "STOP", "power_history": "STOP"})),
                expected_revision=failed_generation,
            )
        )
        stop_apply = await common_w.ApplyConfigSnapshot(
            common_pb2.ApplyConfigSnapshotRequest(
                candidate_id=stop_push.candidate_id, expected_revision=failed_generation
            )
        )
        assert stop_apply.effective_revision == stop_apply.file_commit
        assert {r.object_id: r.status for r in stop_apply.apply_results} == {"ems_power": "APPLIED"}
        await _wait_runtime(env, env.writer_port, stop_apply.file_commit)
        await _wait_runtime(env, env.reader_port, stop_apply.file_commit)

        # --- Cross-process Start: accepted intent, honestly PENDING (RPC 12) --
        op = await sink_w.StartDelivery(
            common_pb2.OperationRequest(
                id="power_history", expected_revision=stop_apply.file_commit
            )
        )
        assert op.accepted is True
        assert op.desired_state == "START"
        assert op.actual_state == "PENDING"

        # The Reader cannot converge the reliable START either; health must
        # degrade instead of pretending success (RPC 2).
        await asyncio.sleep(1.0)
        reader_status = await common_r.GetServiceStatus(common_pb2.Empty())
        assert reader_status.health == "DEGRADED"
        dependency = {item.name: item.health for item in reader_status.dependencies}
        assert dependency == {"power_history": "FAILED"}

        # --- Idempotent in-process Stop (RPC 13) ------------------------------
        stopped = await sink_w.StopDelivery(
            common_pb2.OperationRequest(id="ems_power", expected_revision=op.revision)
        )
        assert stopped.accepted is True
        assert stopped.desired_state == "STOP"
        assert stopped.actual_state == "STOPPED"

        # --- Queries: filter, paging, NOT_FOUND, argument validation (10/11) --
        listing = await sink_w.ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest(channel="realtime", page_size=1)
        )
        assert [item.delivery_id for item in listing.deliveries] == ["ems_power"]
        assert listing.next_page_token == ""
        assert (
            await sink_w.ListDeliveries(sink_service_pb2.ListDeliveriesRequest(channel="reliable"))
        ).deliveries == []
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await sink_w.GetDelivery(common_pb2.ObjectId(id="ghost"))
        assert excinfo.value.code() == grpc.StatusCode.NOT_FOUND
        with pytest.raises(grpc.aio.AioRpcError) as excinfo:
            await sink_w.ListDeliveries(sink_service_pb2.ListDeliveriesRequest(page_size=501))
        assert excinfo.value.code() == grpc.StatusCode.INVALID_ARGUMENT

    # --- Both processes leave SIGTERM with a zero exit code -------------------
    for proc in (writer, reader):
        proc.send_signal(signal.SIGTERM)
    for proc in (writer, reader):
        try:
            assert proc.wait(timeout=15) == 0
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
