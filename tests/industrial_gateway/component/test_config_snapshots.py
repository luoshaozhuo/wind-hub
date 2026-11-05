"""CommonService 配置快照三态/CAS/原子应用/热重载/端口交换契约（真实 gRPC）。

仅 Gateway 自有 Service 文件可被 Push/Apply；共享实例与模板文件永不被替换。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gwpb
from tests.support.gateway_v1 import (
    reserve_port,
    start_gateway_v1,
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
    gateway = await start_gateway_v1(tmp_path / "root", two_server_specs())
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


async def test_push_validates_cas_file_set_integrity_and_config(harness) -> None:
    stub = harness.common_stub()
    revision, content, relative_path = await _disk_revision(stub)

    # CAS：基线 revision 不符。
    good = _server_content(content, lambda s: s)
    conflict = await _push(stub, good, relative_path, "stale-revision")
    assert not conflict.valid
    assert {issue.code for issue in conflict.validation} == {"REVISION_CONFLICT"}

    # 文件集：两个文件 / 路径不属于本 Service。
    reply = await stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[
                _snapshot_file(relative_path, good),
                _snapshot_file(relative_path, good),
            ],
            expected_revision=revision,
        )
    )
    assert {issue.code for issue in reply.validation} == {"FILE_SET"}
    reply = await _push(stub, good, "instances/devices.yaml", revision)
    assert {issue.code for issue in reply.validation} == {"FILE_SET"}

    # 完整性：sha256 不符、空内容、超 2 MiB。
    bad_digest = common.PushConfigSnapshotRequest(
        files=[
            common.SnapshotFile(
                relative_path=relative_path,
                content=good,
                size_bytes=len(good),
                sha256="0" * 64,
            )
        ],
        expected_revision=revision,
    )
    reply = await stub.PushConfigSnapshot(bad_digest)
    assert {issue.code for issue in reply.validation} == {"CONTENT_INTEGRITY"}
    # size_bytes 与实际内容不符：CONTENT_INTEGRITY。
    lying_size = common.SnapshotFile(
        relative_path=relative_path,
        content=good,
        size_bytes=len(good) + 1,
        sha256=hashlib.sha256(good).hexdigest(),
    )
    reply = await stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(files=[lying_size], expected_revision=revision)
    )
    assert {issue.code for issue in reply.validation} == {"CONTENT_INTEGRITY"}
    # 超过 2 MiB 的消息在传输层即被 request_max_bytes 拒绝（服务边界硬限）。
    oversized = _snapshot_file(relative_path, b"a" * (2 * 1024 * 1024 + 1))
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.PushConfigSnapshot(
            common.PushConfigSnapshotRequest(files=[oversized], expected_revision=revision)
        )
    assert exc.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED

    # 配置语义：删掉所有 servers。
    invalid = _mutate(good, servers=[])
    reply = await _push(stub, invalid, relative_path, revision)
    assert not reply.valid and reply.validation[0].code == "INVALID_CONFIG"

    # Push 不落盘：磁盘 revision 不变。
    assert (await _disk_revision(stub))[0] == revision


async def test_candidate_scope_roundtrip_and_unknown_candidate(harness) -> None:
    stub = harness.common_stub()
    revision, content, relative_path = await _disk_revision(stub)
    candidate_content = _server_content(
        content,
        lambda s: (s.update(desired_state="STOP") if s["server_id"] == "gw2" else s) or s,
    )
    pushed = await _push(stub, candidate_content, relative_path, revision)
    assert pushed.valid and pushed.candidate_id
    fetched = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(
            scope=common.SNAPSHOT_SCOPE_CANDIDATE, candidate_id=pushed.candidate_id
        )
    )
    assert fetched.files[0].sha256 == hashlib.sha256(candidate_content).hexdigest()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(
                scope=common.SNAPSHOT_SCOPE_CANDIDATE, candidate_id="ghost"
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_apply_hot_reconfigures_desired_state(harness) -> None:
    stub = harness.common_stub()
    revision, content, relative_path = await _disk_revision(stub)

    def stop_gw2(server):
        if server["server_id"] == "gw2":
            server["desired_state"] = "STOP"
        return server

    candidate = await _push(stub, _server_content(content, stop_gw2), relative_path, revision)
    applied = await stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=candidate.candidate_id,
            expected_revision=revision,
        )
    )
    assert not applied.restart_required
    statuses = {result.object_id: result.status for result in applied.apply_results}
    assert statuses["gw1"] == "UNCHANGED"
    assert statuses["gw2"] == "APPLIED"
    gw2 = await harness.gateway_stub().GetGateway(common.ObjectId(id="gw2"))
    assert gw2.gateway.actual_state == "STOPPED"
    # 磁盘已提交且运行时三态一致。
    new_disk = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
    )
    new_runtime = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    assert new_disk.revision == applied.effective_revision
    assert new_runtime.revision == applied.effective_revision
    # 候选在 Apply 后清空。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(
                scope=common.SNAPSHOT_SCOPE_CANDIDATE,
                candidate_id=candidate.candidate_id,
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_apply_stale_revision_aborted_and_unknown_candidate_not_found(
    harness,
) -> None:
    stub = harness.common_stub()
    revision, content, relative_path = await _disk_revision(stub)
    candidate = await _push(stub, _server_content(content, lambda s: s), relative_path, revision)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id=candidate.candidate_id,
                expected_revision="stale",
            )
        )
    assert exc.value.code() is grpc.StatusCode.ABORTED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ApplyConfigSnapshot(
            common.ApplyConfigSnapshotRequest(
                candidate_id="ghost",
                expected_revision=revision,
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_apply_service_grpc_change_requires_restart(harness) -> None:
    stub = harness.common_stub()
    revision, content, _relative_path = await _disk_revision(stub)
    new_grpc_port = reserve_port()
    candidate_content = _mutate(content)
    document = yaml.safe_load(candidate_content)
    document["grpc"]["listen_port"] = new_grpc_port
    candidate_content = yaml.safe_dump(document).encode()
    candidate = await stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[_snapshot_file(_relative_path, candidate_content)],
            expected_revision=revision,
        )
    )
    assert candidate.valid
    applied = await stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=candidate.candidate_id,
            expected_revision=revision,
        )
    )
    assert applied.restart_required
    service_results = [result for result in applied.apply_results if result.object_id == "service"]
    assert service_results and service_results[0].status == "RESTART_REQUIRED"
    # 当前 gRPC 通道仍可用（旧监听器未被热切换）。
    info = await stub.GetServiceInfo(common.Empty())
    assert info.service_id == "industrial_gateway"


async def test_apply_port_swap_hot_rebinds_listeners(harness) -> None:
    """gw1/gw2 交换监听端口：先停全部变更监听器再启替换者，热重载成功。"""
    import asyncio
    import struct

    stub = harness.common_stub()
    revision, content, relative_path = await _disk_revision(stub)
    document = yaml.safe_load(content)
    ports = {server["server_id"]: server["listen_port"] for server in document["servers"]}

    def swap(server):
        other = "gw2" if server["server_id"] == "gw1" else "gw1"
        server["listen_port"] = ports[other]
        return server

    candidate = await _push(
        stub,
        _server_content(content, swap),
        relative_path,
        revision,
    )
    applied = await stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=candidate.candidate_id,
            expected_revision=revision,
        )
    )
    assert not applied.restart_required

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

    # gw1（unit 1）现在监听原 gw2 端口，反之亦然。
    body_gw1 = await fc03(ports["gw2"], 1)
    body_gw2 = await fc03(ports["gw1"], 2)
    assert body_gw1[0] == 0x03 and body_gw2[0] == 0x03


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
