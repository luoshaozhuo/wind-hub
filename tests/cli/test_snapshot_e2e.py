"""UC-04 只读配置快照 Get/export 完整性测试（真实子进程）。"""

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
