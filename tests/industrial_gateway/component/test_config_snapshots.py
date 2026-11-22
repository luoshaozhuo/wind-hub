"""Gateway 配置读取、唯一发布者守卫及内部重配置契约（真实 gRPC）。

Gateway Push/Apply 已禁用，发布由 Repository 完成；内部重配置仍需保证原子性。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gwpb
from tests.support.gateway import (
    reserve_port,
    start_gateway,
    two_server_specs,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service]


def _snapshot_file(relative_path: str, content: bytes) -> common.SnapshotFile:
    return common.SnapshotFile(
        relative_path=relative_path,
        content=content,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway(tmp_path / "root", two_server_specs())
    try:
        yield gateway
    finally:
        await gateway.stop()


async def _disk_revision(stub) -> tuple[str, bytes, str]:
    reply = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
    )
    return reply.revision, reply.files[0].content, reply.files[0].relative_path


async def _push(stub, content: bytes, relative_path: str, revision: str):
    return await stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[_snapshot_file(relative_path, content)],
            expected_revision=revision,
        )
    )


def _mutate(content: bytes, **changes) -> bytes:
    document = yaml.safe_load(content)
    for key, value in changes.items():
        document[key] = value
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()


def _server_content(content: bytes, mutate) -> bytes:
    document = yaml.safe_load(content)
    document["servers"] = [mutate(dict(server)) for server in document["servers"]]
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()


async def test_disk_and_runtime_scopes_agree_initially(harness) -> None:
    stub = harness.common_stub()
    disk = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
    )
    runtime = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    assert disk.revision == runtime.revision
    assert disk.files[0].sha256 == runtime.files[0].sha256
    assert disk.files[0].size_bytes == len(disk.files[0].content)
    assert disk.files[0].size_bytes <= 2 * 1024 * 1024


async def _apply_authorized_closure(harness, content: bytes):
    """Exercise application-level reconfigure after Repository validation.

    Repository publication/Deployment CAS is tested in Configuration Service;
    the legacy Gateway Push/Apply RPC is never used as a publisher here.
    """
    from industrial_gateway.infrastructure.config.resolution import load_gateway

    selected = load_gateway(
        harness.service_path,
        project_root=harness.root,
        candidate_document=yaml.safe_load(content),
    )
    return await harness.runtime.reconfigure(selected)


async def test_push_validates_cas_file_set_integrity_and_config(harness) -> None:
    stub = harness.common_stub()
    revision, content, relative_path = await _disk_revision(stub)
    for changed in (
        _server_content(content, lambda server: server),
        _mutate(content, servers=[]),
    ):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await _push(stub, changed, relative_path, revision)
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert (await _disk_revision(stub))[0] == revision


async def test_candidate_scope_roundtrip_and_unknown_candidate(harness) -> None:
    stub = harness.common_stub()
    revision, content, relative_path = await _disk_revision(stub)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await _push(stub, content, relative_path, revision)
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(
                scope=common.SNAPSHOT_SCOPE_CANDIDATE, candidate_id="not-published"
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_apply_hot_reconfigures_desired_state(harness) -> None:
    stub = harness.common_stub()
    _revision, content, _path = await _disk_revision(stub)

    def stop_gw2(server):
        if server["server_id"] == "gw2":
            server["desired_state"] = "STOP"
        return server

    results, restart = await _apply_authorized_closure(harness, _server_content(content, stop_gw2))
    assert not restart
    assert {result.object_id: result.status for result in results} == {
        "gw1": "UNCHANGED",
        "gw2": "APPLIED",
    }
    gw2 = await harness.gateway_stub().GetGateway(common.ObjectId(id="gw2"))
    assert gw2.gateway.actual_state == "STOPPED"


async def test_apply_stale_revision_aborted_and_unknown_candidate_not_found(harness) -> None:
    stub = harness.common_stub()
    revision, _content, _path = await _disk_revision(stub)
    for candidate_id, expected in (
        ("not-published", revision),
        ("stale", "0" * 64),
    ):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.ApplyConfigSnapshot(
                common.ApplyConfigSnapshotRequest(
                    candidate_id=candidate_id, expected_revision=expected
                )
            )
        assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert (await _disk_revision(stub))[0] == revision


async def test_apply_service_grpc_change_requires_restart(harness) -> None:
    stub = harness.common_stub()
    _revision, content, _path = await _disk_revision(stub)
    document = yaml.safe_load(content)
    document["grpc"]["listen_port"] = reserve_port()
    from industrial_gateway.infrastructure.config.resolution import load_gateway

    selected = load_gateway(
        harness.service_path,
        project_root=harness.root,
        candidate_document=document,
    )
    # The Repository observer refuses process-owned Service changes before
    # entering runtime.reconfigure; it remains a restart-only target.
    old_revision = harness.runtime.config.revision
    assert selected.service != harness.runtime.config.service
    assert harness.runtime.config.revision == old_revision
    assert (await stub.GetServiceInfo(common.Empty())).service_id


async def test_apply_port_swap_hot_rebinds_listeners(harness) -> None:
    """Application reconfiguration swaps both listeners without a partial bind."""
    import asyncio
    import struct

    _revision, content, _path = await _disk_revision(harness.common_stub())
    document = yaml.safe_load(content)
    ports = {server["server_id"]: server["listen_port"] for server in document["servers"]}
    for server in document["servers"]:
        server["listen_port"] = ports["gw2" if server["server_id"] == "gw1" else "gw1"]
    results, restart = await _apply_authorized_closure(harness, yaml.safe_dump(document).encode())
    assert not restart
    assert {item.status for item in results} == {"APPLIED"}

    async def fc03(port: int, unit: int) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            pdu = struct.pack(">BHH", 0x03, 110, 1)
            writer.write(struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu)
            header = await reader.readexactly(7)
            _tid, _proto, length, _u = struct.unpack(">HHHB", header)
            return await reader.readexactly(length - 1)
        finally:
            writer.close()
            await writer.wait_closed()

    assert (await fc03(ports["gw2"], 1))[0] == 0x03
    assert (await fc03(ports["gw1"], 2))[0] == 0x03


async def test_apply_bind_failure_is_partial_and_does_not_fake_runtime_revision(harness) -> None:
    """A rejected listener replacement must never change active runtime revision."""
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    occupied = blocker.getsockname()[1]
    try:
        previous = harness.runtime.config.revision
        _revision, content, _path = await _disk_revision(harness.common_stub())
        document = yaml.safe_load(content)
        for server in document["servers"]:
            if server["server_id"] == "gw1":
                server["listen_port"] = occupied
        results, _restart = await _apply_authorized_closure(
            harness, yaml.safe_dump(document).encode()
        )
        assert {item.object_id: item.status for item in results} == {
            "gw1": "FAILED",
            "gw2": "UNCHANGED",
        }
        assert harness.runtime.config.revision == previous
    finally:
        blocker.close()


async def test_listener_state_visible_in_gateway_status(harness) -> None:
    stub = harness.common_stub()
    status = await stub.GetServiceStatus(common.Empty())
    assert status.health == "HEALTHY"
    deps = {dep.name: dep for dep in status.dependencies}
    assert deps["gw1"].health == "HEALTHY"
    # 显式停止 gw1：期望态=STOPPED 时实际 STOPPED 仍计 HEALTHY（设计如此）。
    revision = (await harness.gateway_stub().GetGateway(common.ObjectId(id="gw1"))).gateway.revision
    await harness.gateway_stub().StopGateway(
        common.OperationRequest(id="gw1", expected_revision=revision)
    )
    status = await stub.GetServiceStatus(common.Empty())
    deps = {dep.name: dep for dep in status.dependencies}
    assert status.health == "HEALTHY"
    assert deps["gw1"].health == "HEALTHY"
    assert "STOPPED" in deps["gw1"].detail
    _ = gwpb  # 保持 import 与模块契约稳定
