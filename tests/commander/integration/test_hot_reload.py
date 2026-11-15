"""§14.3 test_hot_reload.py（integration/reliability）：配置快照三 scope、

先验完整性、路径归一化、非法 YAML/TLS 失败、乐观修订冲突、版本化模板
不可变、文件原子替换、引用闭包热更新、undo log 崩溃恢复与推送并发。

链路全部真实：gRPC mTLS + ConfigSnapshots + 真实共享配置树 +
fsync undo log + build_commander reload；无任何文件系统替身。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import uuid

import grpc
import pytest
import yaml

from commander.infrastructure.config.config_commit import (
    _MARKER,
    _STORE,
    commit_config_files,
    recover_config_commit,
)
from contracts import common_pb2

pytestmark = [
    pytest.mark.integration,
    pytest.mark.reliability,
    pytest.mark.real_service,
]

DISK = common_pb2.SNAPSHOT_SCOPE_DISK
RUNTIME = common_pb2.SNAPSHOT_SCOPE_RUNTIME
CANDIDATE = common_pb2.SNAPSHOT_SCOPE_CANDIDATE


def _snapshot_file(name: str, content: bytes) -> common_pb2.SnapshotFile:
    return common_pb2.SnapshotFile(
        relative_path=name,
        content=content,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def _aborts(call, code: grpc.StatusCode) -> grpc.aio.AioRpcError:
    with pytest.raises(grpc.aio.AioRpcError) as exc_info:
        await call
    assert exc_info.value.code() is code, exc_info.value
    return exc_info.value


async def disk_snapshot(common) -> common_pb2.GetConfigSnapshotResponse:
    response = await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=DISK), timeout=10
    )
    return response


def _names(response: common_pb2.GetConfigSnapshotResponse) -> dict[str, bytes]:
    return {f.relative_path: bytes(f.content) for f in response.files}


def _service_name(response: common_pb2.GetConfigSnapshotResponse) -> str:
    return next(name for name in _names(response) if name.endswith("commander.yaml"))


def _devices_name(response: common_pb2.GetConfigSnapshotResponse) -> str:
    return next(name for name in _names(response) if name.endswith("devices.yaml"))


def _versioned_template_name(response: common_pb2.GetConfigSnapshotResponse) -> str:
    return next(
        name for name in _names(response) if "/point_tables/" in name and name.endswith(".yaml")
    )


async def _push(
    common,
    files: dict[str, bytes],
    revision: str,
    *,
    timeout: float = 30,
) -> common_pb2.PushConfigSnapshotResponse:
    return await common.PushConfigSnapshot(
        common_pb2.PushConfigSnapshotRequest(
            files=[_snapshot_file(name, content) for name, content in files.items()],
            expected_revision=revision,
        ),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# 三 scope 与候选生命周期
# ---------------------------------------------------------------------------


@pytest.mark.normal
async def test_disk_runtime_candidate_scopes_are_distinct(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    runtime = await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=RUNTIME), timeout=10
    )
    assert len(disk.revision) == 64
    assert disk.revision != runtime.revision  # RUNTIME 是归一化表示，不冒充磁盘
    assert disk.snapshot_id != runtime.snapshot_id
    assert {r.object_id for r in runtime.apply_results}
    pushed = await _push(common, _names(disk), disk.revision)
    assert pushed.valid and pushed.candidate_id
    assert pushed.base_revision == disk.revision
    candidate = await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=CANDIDATE, candidate_id=pushed.candidate_id),
        timeout=10,
    )
    assert candidate.revision == disk.revision
    # 候选绝不等于生效版本：推送期间 DISK 摘要不变。
    again = await disk_snapshot(common)
    assert again.revision == disk.revision


@pytest.mark.exception
async def test_get_unknown_scope_and_candidate_rejected(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    await _aborts(
        common.GetConfigSnapshot(common_pb2.GetConfigSnapshotRequest(scope=0), timeout=5),
        grpc.StatusCode.INVALID_ARGUMENT,
    )
    await _aborts(
        common.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(scope=CANDIDATE, candidate_id="nope"),
            timeout=5,
        ),
        grpc.StatusCode.NOT_FOUND,
    )


# ---------------------------------------------------------------------------
# 路径归一化 / 先验完整性（在解析任何候选 YAML 之前）
# ---------------------------------------------------------------------------


@pytest.mark.exception
@pytest.mark.parametrize(
    "bad_path",
    [
        "",
        "../escape.yaml",
        "config/../escape.yaml",
        "/etc/passwd",
        "./commander.yaml",
        "config//instances/devices.yaml",
        ".hidden.yaml",
        "config/.secret/devices.yaml",
    ],
)
async def test_push_rejects_noncanonical_or_escaping_paths(pki, grpc_stack, bad_path) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    files[bad_path] = b"x: 1\n"
    await _aborts(_push(common, files, disk.revision), grpc.StatusCode.INVALID_ARGUMENT)


@pytest.mark.exception
async def test_push_rejects_duplicate_relative_path(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    request = common_pb2.PushConfigSnapshotRequest(
        expected_revision=disk.revision,
        files=[disk.files[0], disk.files[0]],
    )
    await _aborts(common.PushConfigSnapshot(request, timeout=10), grpc.StatusCode.INVALID_ARGUMENT)


@pytest.mark.exception
@pytest.mark.parametrize("tamper", ["size", "sha"])
async def test_push_verifies_size_and_sha_before_yaml_parse(pki, grpc_stack, tamper) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    item = disk.files[0]
    kwargs = {
        "relative_path": item.relative_path,
        "content": item.content,
        "size_bytes": item.size_bytes + (1 if tamper == "size" else 0),
        "sha256": ("0" * 64 if tamper == "sha" else item.sha256),
    }
    request = common_pb2.PushConfigSnapshotRequest(
        expected_revision=disk.revision, files=[common_pb2.SnapshotFile(**kwargs)]
    )
    await _aborts(common.PushConfigSnapshot(request, timeout=10), grpc.StatusCode.INVALID_ARGUMENT)


@pytest.mark.exception
async def test_push_stale_expected_revision_aborted(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    await _aborts(
        _push(common, _names(disk), "0" * 64),
        grpc.StatusCode.ABORTED,
    )


# ---------------------------------------------------------------------------
# 非法候选 YAML / TLS 失败：返回 valid=False 的 ValidationIssue，不改磁盘
# ---------------------------------------------------------------------------


@pytest.mark.exception
async def test_push_malformed_service_yaml_rejected_before_staging(pki, grpc_stack) -> None:
    """服务 YAML 在暂存前的预解析非法：INVALID_ARGUMENT，候选不建立。"""
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    files[_service_name(disk)] = b"schema_version: [\n  : bad"
    err = await _aborts(_push(common, files, disk.revision), grpc.StatusCode.INVALID_ARGUMENT)
    assert "yaml" in err.details().lower()
    assert (await disk_snapshot(common)).revision == disk.revision


@pytest.mark.exception
async def test_push_malformed_shared_devices_yaml_returns_validation_issue(pki, grpc_stack) -> None:
    """共享配置 YAML 在隔离暂存区解析失败：valid=False + INVALID_CONFIG（435b833c）。"""
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    files[_devices_name(disk)] = b"schema_version: [\ndevices: : bad\n"
    pushed = await _push(common, files, disk.revision)
    assert pushed.valid is False
    assert pushed.validation[0].code == "INVALID_CONFIG"
    assert (await disk_snapshot(common)).revision == disk.revision


@pytest.mark.exception
async def test_push_service_yaml_with_broken_tls_paths_rejected(pki, grpc_stack) -> None:
    """候选在暂存区做真实 SSLContext 装载校验：证书路径不存在→valid=False。"""
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    files[_service_name(disk)] = (
        b"schema_version: 1\nfile_version: 1.0.0\n"
        b"security:\n  ca_cert: /nonexistent/ca.pem\n"
        b"  server_cert: /nonexistent/server.pem\n"
        b"  server_key: /nonexistent/server.key\n"
        b"config_sources:\n  instances:\n    devices: "
        + _devices_name(disk).replace("config/", "", 1).encode()
        + b"\n  templates:\n    root: config/templates\n"
    )
    pushed = await _push(common, files, disk.revision)
    assert pushed.valid is False
    issue = pushed.validation[0]
    assert issue.code == "INVALID_CONFIG"


@pytest.mark.exception
async def test_push_missing_referenced_devices_file_rejected(pki, grpc_stack) -> None:
    """引用闭包缺失：路径不存在时不得默默回退默认目录（§11/§14.1 强制）。"""
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    document = yaml.safe_load(files[_service_name(disk)])
    document["config_sources"]["instances"]["devices"] = "config/instances/missing.yaml"
    files[_service_name(disk)] = yaml.safe_dump(document).encode()
    pushed = await _push(common, files, disk.revision)
    assert pushed.valid is False
    assert pushed.validation[0].code == "INVALID_CONFIG"


@pytest.mark.normal
async def test_push_absolute_config_source_path_validates(pki, grpc_stack) -> None:
    """绝对 pathname 保持原值并在隔离暂存区映射校验（§11）。"""
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    document = yaml.safe_load(files[_service_name(disk)])
    absolute_devices = grpc_stack.root / _devices_name(disk)
    document["config_sources"]["instances"]["devices"] = str(absolute_devices)
    files[_service_name(disk)] = yaml.safe_dump(document).encode()
    pushed = await _push(common, files, disk.revision)
    assert pushed.valid is True, pushed.validation


# ---------------------------------------------------------------------------
# 版本化模板不可变 / 未引用模板不加载
# ---------------------------------------------------------------------------


@pytest.mark.exception
async def test_push_cannot_modify_existing_versioned_template(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    template = _versioned_template_name(disk)
    files = _names(disk)
    files[template] = files[template] + b"# mutation\n"
    err = await _aborts(_push(common, files, disk.revision), grpc.StatusCode.INVALID_ARGUMENT)
    assert "immutable" in err.details()


@pytest.mark.normal
async def test_unreferenced_new_template_does_not_reload_devices(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    existing = _versioned_template_name(disk)
    new_template = existing.rsplit("/", 1)[0] + "/orphan_wtg_9.9.9.yaml"
    files[new_template] = b"schema_version: 1\n"
    pushed = await _push(common, files, disk.revision)
    assert pushed.valid is True, pushed.validation
    applied = await common.ApplyConfigSnapshot(
        common_pb2.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id, expected_revision=disk.revision
        ),
        timeout=30,
    )
    # 无设备引用新模板：没有任何设备出现在逐对象结果中。
    assert applied.apply_results == []
    assert applied.restart_required is False
    assert (grpc_stack.root / new_template).is_file()
    # 已落盘的新版本模板即刻不可变。
    again = await disk_snapshot(common)
    files2 = _names(again)
    files2[new_template] = b"schema_version: 2\n"
    err = await _aborts(_push(common, files2, again.revision), grpc.StatusCode.INVALID_ARGUMENT)
    assert "immutable" in err.details()


# ---------------------------------------------------------------------------
# Apply：文件替换、修订冲突、候选一次性、服务级变更需重启、热更新范围
# ---------------------------------------------------------------------------


@pytest.mark.normal
async def test_apply_replaces_files_without_semantic_reload_for_comment(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    devices = _devices_name(disk)
    files[devices] = files[devices] + b"# cosmetic trailing comment\n"
    pushed = await _push(common, files, disk.revision)
    applied = await common.ApplyConfigSnapshot(
        common_pb2.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id, expected_revision=disk.revision
        ),
        timeout=30,
    )
    assert applied.restart_required is False
    assert applied.file_commit == (await disk_snapshot(common)).revision
    assert applied.file_commit != disk.revision
    # RUNTIME 设备绑定修订不受无语义字节替换影响。
    runtime = await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=RUNTIME), timeout=10
    )
    assert runtime.revision


@pytest.mark.normal
async def test_apply_service_level_write_deadline_change_requires_restart(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    service = _service_name(disk)
    assert b"write_default_ms: 3000" in files[service]
    files[service] = files[service].replace(b"write_default_ms: 3000", b"write_default_ms: 2500")
    pushed = await _push(common, files, disk.revision)
    assert pushed.valid is True, pushed.validation
    applied = await common.ApplyConfigSnapshot(
        common_pb2.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id, expected_revision=disk.revision
        ),
        timeout=30,
    )
    assert applied.restart_required is True


@pytest.mark.normal
async def test_apply_hot_change_affects_only_referencing_device(pki, grpc_stack) -> None:
    """只改 TZ05 端口：逐对象结果仅含 TZ05，RUNTIME 归一化只反映该设备。"""
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    devices_name = _devices_name(disk)
    document = yaml.safe_load(files[devices_name])
    old_port = document["devices"]["TZ05"]["endpoint"]["port"]
    document["devices"]["TZ05"]["endpoint"]["port"] = old_port + 4000
    files[devices_name] = yaml.safe_dump(document, sort_keys=False).encode()
    pushed = await _push(common, files, disk.revision)
    assert pushed.valid is True, pushed.validation
    applied = await common.ApplyConfigSnapshot(
        common_pb2.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id, expected_revision=disk.revision
        ),
        timeout=30,
    )
    objects = {r.object_id: r.status for r in applied.apply_results}
    assert objects == {"TZ05": "APPLIED"}
    runtime = await common.GetConfigSnapshot(
        common_pb2.GetConfigSnapshotRequest(scope=RUNTIME), timeout=10
    )
    normalized = _names(runtime)
    runtime_devices = yaml.safe_load(normalized[devices_name])["devices"]
    assert runtime_devices["TZ05"]["endpoint"]["port"] == old_port + 4000
    # 热更新只影响引用设备：TZ01 端口保持 1502。
    assert runtime_devices["TZ01"]["endpoint"]["port"] == 1502


@pytest.mark.exception
async def test_apply_conflict_when_disk_changed_externally(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    pushed = await _push(common, _names(disk), disk.revision)
    # 候选两次操作之间，磁盘被外部修改（独立行注释，保持 YAML 可解析，
    # 只改变摘要而不破坏引用闭包，使冲突检查先于重校验触发）。
    devices = grpc_stack.root / _devices_name(disk)
    devices.write_bytes(devices.read_bytes() + b"\n# external edit\n")
    await _aborts(
        common.ApplyConfigSnapshot(
            common_pb2.ApplyConfigSnapshotRequest(
                candidate_id=pushed.candidate_id, expected_revision=disk.revision
            ),
            timeout=30,
        ),
        grpc.StatusCode.ABORTED,
    )


@pytest.mark.exception
async def test_candidate_is_single_use_and_unknown_id_not_found(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    pushed = await _push(common, _names(disk), disk.revision)
    applied = await common.ApplyConfigSnapshot(
        common_pb2.ApplyConfigSnapshotRequest(
            candidate_id=pushed.candidate_id, expected_revision=disk.revision
        ),
        timeout=30,
    )
    assert applied.restart_required is False
    await _aborts(
        common.ApplyConfigSnapshot(
            common_pb2.ApplyConfigSnapshotRequest(
                candidate_id=pushed.candidate_id, expected_revision=disk.revision
            ),
            timeout=10,
        ),
        grpc.StatusCode.NOT_FOUND,
    )


# ---------------------------------------------------------------------------
# 并发：推送串行化，候选不被交叉污染
# ---------------------------------------------------------------------------


@pytest.mark.concurrency
async def test_concurrent_pushes_serialize_and_single_candidate(pki, grpc_stack) -> None:
    common, _commander = grpc_stack.stubs(pki, client="deployer")
    disk = await disk_snapshot(common)
    files = _names(disk)
    pushes = await asyncio.gather(
        _push(common, dict(files), disk.revision),
        _push(common, dict(files), disk.revision),
    )
    assert all(response.valid for response in pushes)
    candidate_ids = {response.candidate_id for response in pushes}
    assert len(candidate_ids) == 2
    surviving = [
        candidate_id
        for candidate_id in candidate_ids
        if await _candidate_exists(common, candidate_id)
    ]
    # 后写胜出：服务端仅保留一个候选。
    assert len(surviving) == 1


async def _candidate_exists(common, candidate_id: str) -> bool:
    try:
        await common.GetConfigSnapshot(
            common_pb2.GetConfigSnapshotRequest(scope=CANDIDATE, candidate_id=candidate_id),
            timeout=5,
        )
    except grpc.aio.AioRpcError as exc:
        if exc.code() is grpc.StatusCode.NOT_FOUND:
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# undo log 崩溃恢复（真实 fsync 日志，直接在 fixture 的配置根上演练）
# ---------------------------------------------------------------------------


@pytest.mark.recovery
async def test_armed_undo_log_rolls_back_on_recovery(pki, grpc_stack) -> None:
    """模拟在 marker 删除前崩溃：after 已替换落盘，重启恢复必须还原 before。"""
    root = grpc_stack.root
    devices = grpc_stack.yaml_path.parent / "config" / "instances" / "devices.yaml"
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
