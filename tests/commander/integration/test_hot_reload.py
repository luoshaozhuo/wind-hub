"""Commander configuration boundary through real authenticated gRPC.

Only Configuration Repository publishes immutable documents and Deployment
selections. Commander exposes disk/runtime snapshots but rejects the retired
PushConfigSnapshot / ApplyConfigSnapshot write path, including malformed,
concurrent and stale requests. Repository-side CAS, immutable publication and
dependency closure are covered by tests/configuration_service.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import uuid

import grpc
import pytest

from commander.infrastructure.config.config_commit import (
    _MARKER,
    _STORE,
    commit_config_files,
    recover_config_commit,
)
from contracts import common_pb2

pytestmark = [pytest.mark.integration, pytest.mark.reliability, pytest.mark.real_service]

DISK = common_pb2.SNAPSHOT_SCOPE_DISK
RUNTIME = common_pb2.SNAPSHOT_SCOPE_RUNTIME
CANDIDATE = common_pb2.SNAPSHOT_SCOPE_CANDIDATE


def _snapshot_file(path: str, content: bytes) -> common_pb2.SnapshotFile:
    return common_pb2.SnapshotFile(
        relative_path=path,
        content=content,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def _aborts(call, code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as failure:
        await call
    assert failure.value.code() is code
    return failure.value


async def _snapshot(common, scope: int = DISK) -> common_pb2.GetConfigSnapshotResponse:
    return await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=scope), timeout=10
    )


def _names(response: common_pb2.GetConfigSnapshotResponse) -> dict[str, bytes]:
    return {item.relative_path: bytes(item.content) for item in response.files}


@pytest.mark.normal
async def test_disk_runtime_candidate_scopes_are_distinct(pki, grpc_stack) -> None:
    common, _ = grpc_stack.stubs(pki, client="deployer")
    disk = await _snapshot(common)
    runtime = await _snapshot(common, RUNTIME)
    assert len(disk.revision) == 64
    assert disk.snapshot_id != runtime.snapshot_id
    assert any(path.endswith("service_commander_v1.0.0.yaml") for path in _names(disk))
    assert any(path.endswith("device_farm-a_v1.4.0.yaml") for path in _names(disk))
    assert {result.object_id for result in runtime.apply_results}


@pytest.mark.exception
async def test_get_unknown_scope_and_candidate_rejected(pki, grpc_stack) -> None:
    common, _ = grpc_stack.stubs(pki, client="deployer")
    await _aborts(
        common.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(scope=0), timeout=5
        ),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        common.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(
                scope=CANDIDATE, candidate_id="no-published-candidate"
            ),
            timeout=5,
        ),
        grpc.StatusCode.NOT_FOUND,
    )


@pytest.mark.exception
@pytest.mark.parametrize(
    "path",
    (
        "",
        "../escape.yaml",
        "config/../escape.yaml",
        "/etc/passwd",
        "./service_commander_v1.0.0.yaml",
        "config//instances/device_farm-a_v1.4.0.yaml",
        ".hidden.yaml",
        "configs/.secret/device_farm-a_v1.4.0.yaml",
        "services/service_commander_v1.0.0.yaml",
    ),
)
async def test_retired_push_refuses_all_paths_without_mutation(
    pki, grpc_stack, path: str
) -> None:
    common, _ = grpc_stack.stubs(pki, client="deployer")
    original = await _snapshot(common)
    error = await _aborts(
        common.PushConfigSnapshot(
            common_pb2.PushConfigSnapshotRequest(
                files=[_snapshot_file(path, b"schema_version: 1\n")],
                expected_revision=original.revision,
            ),
            timeout=10,
        ),
        grpc.StatusCode.FAILED_PRECONDITION,
    )
    assert "Repository" in error.details()
    assert (await _snapshot(common)).revision == original.revision


@pytest.mark.exception
@pytest.mark.parametrize("mutation", ("duplicate", "bad_size", "bad_digest", "stale"))
async def test_retired_push_refuses_tampering_and_stale_revisions(
    pki, grpc_stack, mutation: str
) -> None:
    common, _ = grpc_stack.stubs(pki, client="deployer")
    disk = await _snapshot(common)
    files = [disk.files[0]]
    revision = disk.revision
    if mutation == "duplicate":
        files.append(disk.files[0])
    elif mutation == "bad_size":
        invalid = common_pb2.SnapshotFile()
        invalid.CopyFrom(disk.files[0])
        invalid.size_bytes += 1
        files = [invalid]
    elif mutation == "bad_digest":
        invalid = common_pb2.SnapshotFile()
        invalid.CopyFrom(disk.files[0])
        invalid.sha256 = "0" * 64
        files = [invalid]
    else:
        revision = "0" * 64
    await _aborts(
        common.PushConfigSnapshot(
            common_pb2.PushConfigSnapshotRequest(
                expected_revision=revision, files=files
            ),
            timeout=10,
        ),
        grpc.StatusCode.FAILED_PRECONDITION,
    )
    assert (await _snapshot(common)).revision == disk.revision


@pytest.mark.exception
async def test_apply_without_repository_publication_is_rejected(pki, grpc_stack) -> None:
    common, _ = grpc_stack.stubs(pki, client="deployer")
    original = await _snapshot(common)
    await _aborts(
        common.ApplyConfigSnapshot(
            common_pb2.ApplyConfigSnapshotRequest(
                candidate_id="nonexistent", expected_revision=original.revision
            ),
            timeout=10,
        ),
        grpc.StatusCode.FAILED_PRECONDITION,
    )
    assert (await _snapshot(common)).revision == original.revision


@pytest.mark.conflict
async def test_concurrent_retired_pushes_are_all_inert(pki, grpc_stack) -> None:
    common, _ = grpc_stack.stubs(pki, client="deployer")
    disk = await _snapshot(common)
    request = common_pb2.PushConfigSnapshotRequest(
        files=[disk.files[0]], expected_revision=disk.revision
    )
    results = await asyncio.gather(
        *(common.PushConfigSnapshot(request, timeout=10) for _ in range(8)),
        return_exceptions=True,
    )
    assert len(results) == 8
    assert all(
        isinstance(result, grpc.aio.AioRpcError)
        and result.code() is grpc.StatusCode.FAILED_PRECONDITION
        for result in results
    )
    assert (await _snapshot(common)).revision == disk.revision


# ---------------------------------------------------------------------------
# undo log 崩溃恢复（真实 fsync 日志，直接在 fixture 的配置根上演练）
# ---------------------------------------------------------------------------


@pytest.mark.recovery
async def test_armed_undo_log_rolls_back_on_recovery(pki, grpc_stack) -> None:
    """模拟在 marker 删除前崩溃：after 已替换落盘，重启恢复必须还原 before。"""
    root = grpc_stack.root
    devices = grpc_stack.yaml_path.parent.parent / "instances" / "device_farm-a_v1.4.0.yaml"
    assert devices.is_relative_to(root)
    original = devices.read_bytes()
    corrupted = original + b"# crashed mid-commit\n"
    transaction = uuid.uuid4().hex
    folder = root / _STORE / transaction
    (folder / "before" / devices.relative_to(root)).parent.mkdir(parents=True)
    (folder / "after" / devices.relative_to(root)).parent.mkdir(parents=True)
    (folder / "before" / devices.relative_to(root)).write_bytes(original)
    after_path = folder / "after" / devices.relative_to(root)
    after_path.write_bytes(corrupted)
    mode = stat.S_IMODE(devices.stat().st_mode)
    entry = {
        "path": devices.relative_to(root).as_posix(),
        "existed": True,
        "mode": mode,
    }
    (root / _MARKER).write_text(
        json.dumps({"transaction_id": transaction, "files": [entry]}),
        encoding="utf-8",
    )
    # 崩溃窗口：目标文件已是 after 内容，但 marker 仍在。
    devices.write_bytes(corrupted)

    recover_config_commit(root)

    assert devices.read_bytes() == original
    assert not (root / _MARKER).exists()
    assert not folder.exists()


@pytest.mark.recovery
async def test_commit_recovery_is_idempotent_and_empty_commit_is_noop(pki, grpc_stack) -> None:
    root = grpc_stack.root
    recover_config_commit(root)  # 无 marker：幂等
    commit_config_files(root, {})  # 空变更：不产生日志
    assert not (root / _MARKER).exists()
