"""UC-04 只读配置快照 Get/export 完整性测试（真实子进程）。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

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


@pytest.mark.parametrize("with_output", [False, True])
def test_config_get_enforces_configured_limit_in_query_and_export_modes(
    cli_env: object, tmp_path: Path, with_output: bool
) -> None:
    document = yaml.safe_load(cli_env.config_path.read_text(encoding="utf-8"))
    document["cli"]["snapshot_max_bytes"] = 4
    cli_env.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    server = cli_env.server
    method = server.method_path("CommonService", "GetConfigSnapshot")
    server.respond_value(
        method,
        common_pb2.GetConfigSnapshotResponse(
            revision="equal-limit",
            files=[_snapshot_file("a.yaml", b"aa"), _snapshot_file("b.yaml", b"bb")],
        ),
    )
    destination = tmp_path / "equal"
    arguments = ["config", "get", "collector", "--scope", "runtime"]
    if with_output:
        arguments.extend(("--output", str(destination)))
    result = cli_env.subprocess_invoke(*arguments)
    assert result.exit_code == 0, result.stdout
    if with_output:
        assert (destination / "a.yaml").read_bytes() == b"aa"
        assert (destination / "b.yaml").read_bytes() == b"bb"

    server.respond_value(
        method,
        common_pb2.GetConfigSnapshotResponse(
            revision="cumulative-overflow",
            files=[
                _snapshot_file("a.yaml", b"aa"),
                _snapshot_file("b.yaml", b"bb"),
                _snapshot_file("c.yaml", b"c"),
            ],
        ),
    )
    overflow_destination = tmp_path / "overflow"
    overflow_arguments = ["config", "get", "collector", "--scope", "runtime"]
    if with_output:
        overflow_arguments.extend(("--output", str(overflow_destination)))
    rejected = cli_env.subprocess_invoke(*overflow_arguments)
    assert rejected.exit_code == 2, rejected.stdout
    assert rejected.json()["error_code"] == "ValueError"
    assert not overflow_destination.exists()
    assert not list(tmp_path.glob(".snapshot-*"))
