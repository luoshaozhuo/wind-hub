"""UC-04 完整配置快照 Get/Push/Apply System Test（真实子进程）。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from contracts import common_pb2

pytestmark = [pytest.mark.system, pytest.mark.contract]


def _snapshot_file(name: str, payload: bytes) -> Any:
    return common_pb2.SnapshotFile(
        relative_path=name,
        content=payload,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_config_get_export_verified_atomic(cli_env: object, tmp_path: Path) -> None:
    """S01：--output 只落经验签的相对路径文件；SHA-256 不符必须拒绝且不建目录。"""
    server = cli_env.server
    payload = b"devices: []\n"
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(
            snapshot_id="s0",
            revision="r1",
            files=[_snapshot_file("collector/collector.yaml", payload)],
        ),
    )
    destination = tmp_path / "snapshot"
    result = cli_env.subprocess_invoke(
        "config", "get", "collector", "--scope", "runtime", "--output", str(destination)
    )
    assert result.exit_code == 0, result.stdout
    assert (destination / "collector" / "collector.yaml").read_bytes() == payload

    # 已存在目录拒绝覆盖（防误写/符号链接替换）
    again = cli_env.subprocess_invoke(
        "config", "get", "collector", "--scope", "runtime", "--output", str(destination)
    )
    assert again.exit_code == 2, again.stdout


def test_config_get_checksum_mismatch_refused(cli_env: object, tmp_path: Path) -> None:
    """S03：内容与 sha256 不一致 → 2，且目标目录不得被创建。"""
    server = cli_env.server
    bad = common_pb2.SnapshotFile(
        relative_path="a.yaml",
        content=b"x",
        size_bytes=1,
        sha256="0" * 64,
    )
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(revision="r1", files=[bad]),
    )
    destination = tmp_path / "should-not-exist"
    result = cli_env.subprocess_invoke(
        "config", "get", "collector", "--scope", "runtime", "--output", str(destination)
    )
    assert result.exit_code in {1, 2}, result.stdout
    assert not destination.exists()


def test_push_revision_conflict_and_invalid_candidate(cli_env: object, tmp_path: Path) -> None:
    """S02：候选校验失败 valid=False → FAILURE/1；expected_revision 取运行时快照。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(revision="r1"),
    )
    server.respond_value(
        server.method_path("CommonService", "PushConfigSnapshot"),
        common_pb2.PushConfigSnapshotResponse(candidate_id="c9", valid=False, base_revision="r1"),
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "x.yaml").write_text("a: 1\n", encoding="utf-8")
    result = cli_env.subprocess_invoke("config", "push", "collector", str(candidate))
    assert result.exit_code == 1, result.stdout
    assert result.json()["status"] == "FAILURE"
    push_call = [c for c in server.calls if c.path.endswith("/PushConfigSnapshot")][0]
    assert push_call.request.expected_revision == "r1"


def test_apply_partial_restart_required_exit4(cli_env: object) -> None:
    """S03：部分生效 + restart_required → PARTIAL/4，逐对象状态都在信封内。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(revision="r1"),
    )
    server.respond_value(
        server.method_path("CommonService", "ApplyConfigSnapshot"),
        common_pb2.ApplyConfigSnapshotResponse(
            file_commit="COMMITTED",
            apply_results=[
                common_pb2.ApplicationResult(object_id="o1", status="SUCCESS"),
                common_pb2.ApplicationResult(object_id="o2", status="FAILED", error_code="E"),
            ],
            effective_revision="r2",
            restart_required=True,
        ),
    )
    result = cli_env.subprocess_invoke("config", "apply", "collector", "c1", "--yes")
    assert result.exit_code == 4, result.stdout
    assert result.json()["status"] == "PARTIAL"


def test_apply_unavailable_unknown_no_partial_claim(cli_env: object) -> None:
    """S03：Apply 通道断连 → UNKNOWN/3，data 必须为 null，禁止伪称生效或幂等重试。"""
    import grpc

    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(revision="r1"),
    )
    server.fail(
        server.method_path("CommonService", "ApplyConfigSnapshot"),
        grpc.StatusCode.UNAVAILABLE,
        "connection lost",
    )
    result = cli_env.subprocess_invoke("config", "apply", "collector", "c1", "--yes")
    assert result.exit_code == 3, result.stdout
    envelope = result.json()
    assert envelope["status"] == "UNKNOWN"
    assert envelope["data"] is None
    applies = [c for c in server.calls if c.path.endswith("/ApplyConfigSnapshot")]
    assert len(applies) == 1


def test_config_get_runtime_view_with_stale_apply_results_is_success(cli_env: object) -> None:
    """只读 config get 的 runtime 视图可携带历史 apply_results；查询成功必须是 0，
    只有本次 ApplyConfigSnapshot 响应才允许按 apply_results 判 PARTIAL。"""
    server = cli_env.server
    server.respond_value(
        server.method_path("CommonService", "GetConfigSnapshot"),
        common_pb2.GetConfigSnapshotResponse(
            revision="r1",
            apply_results=[
                common_pb2.ApplicationResult(object_id="o1", status="SUCCESS"),
                common_pb2.ApplicationResult(object_id="o2", status="FAILED", error_code="E"),
            ],
        ),
    )
    result = cli_env.subprocess_invoke("config", "get", "collector", "--scope", "runtime")
    assert result.exit_code == 0, result.stdout
