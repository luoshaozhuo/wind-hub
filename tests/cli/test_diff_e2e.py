"""UC-05 本地完整配置目录比较 System Test（真实子进程、真实文件系统）。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
pytestmark = [pytest.mark.system, pytest.mark.integration]


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _diff(config: Path, left: Path, right: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cli",
            "--config",
            str(config),
            "--format",
            "json",
            "config",
            "diff",
            str(left),
            str(right),
        ],
        cwd=SRC,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_diff_add_delete_modify_and_summary(cli_env: object, tmp_path: Path) -> None:
    """S01：增删改三类差异 + 容量统计（config diff 为纯本地命令）。"""
    left, right = tmp_path / "left", tmp_path / "right"
    _tree(left, {"same.yaml": "a: 1\n", "deleted.yaml": "x: 0\n", "changed.yaml": "v: 1\n"})
    _tree(
        right,
        {"same.yaml": "a: 1\n", "added.yaml": "y: 2\n", "changed.yaml": "v: 2\n"},
    )
    result = cli_env.subprocess_invoke("config", "diff", str(left), str(right))
    assert result.exit_code == 0, result.stdout
    data = result.json()["data"]
    assert data["status"] == "COMPLETE"
    assert data["added"] == ["added.yaml"]
    assert data["removed"] == ["deleted.yaml"]
    assert data["modified"] == ["changed.yaml"]
    assert data["left_files"] == 3
    assert data["right_files"] == 3


def test_diff_empty_directories_complete(cli_env: object, tmp_path: Path) -> None:
    """S02：两个空目录（含空子目录）→ COMPLETE/0。"""
    left, right = tmp_path / "l", tmp_path / "r"
    (left / "emptysub").mkdir(parents=True)
    (right / "emptysub").mkdir(parents=True)
    result = cli_env.subprocess_invoke("config", "diff", str(left), str(right))
    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["status"] == "COMPLETE"


def test_diff_changed_during_scan_exit4(cli_env: object, tmp_path: Path) -> None:
    """S03：读取窗口内文件大小发生变化 → CHANGED_DURING_SCAN/4（真实并发写者）。

    右侧放大文件，读哈希需要可观察窗口；写线程在子进程扫描期间持续追加，
    stat 前后不一致必须被识别，绝不输出伪 COMPLETE。
    """
    left, right = tmp_path / "l", tmp_path / "r"
    left.mkdir()
    right.mkdir()
    moving = right / "moving.yaml"
    moving.write_bytes(b"a: " + b"0" * (32 * 1024 * 1024) + b"\n")
    stop = threading.Event()

    def writer() -> None:
        with moving.open("ab") as handle:
            while not stop.is_set():
                handle.write(b"0")
                handle.flush()
                os.fsync(handle.fileno())

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        completed = _diff(cli_env.config_path, left, right)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert completed.returncode == 4, completed.stdout
    assert "CHANGED_DURING_SCAN" in completed.stdout


def test_diff_truncated_output_incomplete_exit4(cli_env: object, tmp_path: Path) -> None:
    """S02：文本 diff 超过输出上限 → INCOMPLETE/4 且 truncation 显式可见。"""
    left, right = tmp_path / "l", tmp_path / "r"
    left.mkdir()
    right.mkdir()
    (left / "big.yaml").write_text("\n".join(f"k{i}: 0" for i in range(12000)))
    (right / "big.yaml").write_text("\n".join(f"k{i}: 1" for i in range(12000)))
    result = cli_env.subprocess_invoke("config", "diff", str(left), str(right))
    assert result.exit_code == 4, result.stdout
    data = result.json()["data"]
    assert data["status"] == "INCOMPLETE"
    assert data["text_diff_truncated"] is True


def test_diff_unreadable_file_and_secret_redaction(cli_env: object, tmp_path: Path) -> None:
    """S03：不可读文件 → INCOMPLETE/4；敏感文件原文绝不进入输出。"""
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    left, right = tmp_path / "l", tmp_path / "r"
    left.mkdir()
    right.mkdir()
    secret = right / "secret.yaml"
    secret.write_text("password: supersecret-token-value\n", encoding="utf-8")
    secret.chmod(0o000)
    try:
        result = cli_env.subprocess_invoke("config", "diff", str(left), str(right))
        assert result.exit_code == 4, result.stdout
        assert result.json()["data"]["status"] in {
            "INCOMPLETE",
            "CHANGED_DURING_SCAN",
        }
        assert "supersecret-token-value" not in result.stdout
    finally:
        secret.chmod(0o644)

    # 可读的 .pem 两侧内容不同：差异可见但内容整体脱敏
    left_key, right_key = left / "cert.pem", right / "cert.pem"
    left_key.write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n", encoding="utf-8")
    right_key.write_text("-----BEGIN PRIVATE KEY-----\nBBBB\n", encoding="utf-8")
    redacted = cli_env.subprocess_invoke("config", "diff", str(left), str(right))
    assert redacted.exit_code == 0, redacted.stdout
    assert "AAAA" not in redacted.stdout
    assert "BBBB" not in redacted.stdout
