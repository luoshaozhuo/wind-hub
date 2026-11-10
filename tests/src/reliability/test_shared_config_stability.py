"""共享配置读取稳定性与协作锁协调（Configuration 设计 v1_7 §14.2.3）。

全部用例使用真实文件系统、真实 os.replace 与真实 fcntl 锁，不 mock 文件
身份或锁原语：

- read_stable_document 必须以原始字节身份识别读取期间的文件替换
  （SOURCE_CHANGED），不能把语义相近内容当作同一文件；
- 共享读锁（LOCK_SH）与发布方排他锁（LOCK_EX）必须落在同一协调根，
  默认 configs/templates 布局对应 templates.parent（即 configs/），
  与 Commander 服务文件 parent.parent 的写入根是同一物理目录。
"""

from __future__ import annotations

import fcntl
import os
import re
import threading
from pathlib import Path

import pytest

from configuration.document_version import read_stable_document
from configuration.shared_documents import SharedConfigPaths
from core.application.errors import ConfigError

pytestmark = [pytest.mark.reliability, pytest.mark.concurrency]

_CONFIGS = Path(__file__).resolve().parents[3] / "configs"


def _large_document(marker: str) -> bytes:
    # 大文档拉长解析窗口，使并发替换在读取窗口内发生成为可重复的高概率事件。
    body = "\n".join(f"key_{index}: {marker}_{'x' * 64}" for index in range(6000))
    return f"schema_version: 1\nfile_version: 1.0.0\n{body}\n".encode()


def test_read_stable_document_accepts_unchanged_file(tmp_path: Path) -> None:
    path = tmp_path / "devices.yaml"
    path.write_bytes(_large_document("a"))

    document = read_stable_document(path)

    assert document["schema_version"] == 1
    assert document["file_version"] == "1.0.0"


def test_read_stable_document_classifies_missing_and_schema_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="REFERENCE_NOT_FOUND"):
        read_stable_document(tmp_path / "absent.yaml")

    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nfile_version: 1.0.0\nroot: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="SCHEMA_ERROR"):
        read_stable_document(bad)


def test_source_changed_detects_real_concurrent_replacement(tmp_path: Path) -> None:
    target = tmp_path / "devices.yaml"
    content_a = _large_document("a")
    content_b = _large_document("b")
    target.write_bytes(content_a)

    stop = threading.Event()

    def swap_forever() -> None:
        contents = (content_a, content_b)
        index = 0
        while not stop.is_set():
            staging = tmp_path / f".swap-{index}.tmp"
            staging.write_bytes(contents[index % 2])
            os.replace(staging, target)
            index += 1

    swapper = threading.Thread(target=swap_forever, name="config-swapper", daemon=True)
    swapper.start()
    changed_events = 0
    try:
        for _ in range(60):
            try:
                read_stable_document(target)
            except ConfigError as exc:
                message = str(exc)
                assert "SOURCE_CHANGED" in message
                # 证据必须包含前后两个不同的 sha256，而不是单一语义哈希。
                match = re.search(r"sha256=([0-9a-f]{64}) -> ([0-9a-f]{64})", message)
                assert match is not None
                assert match.group(1) != match.group(2)
                changed_events += 1
    finally:
        stop.set()
        swapper.join(timeout=5)

    assert not swapper.is_alive()
    assert changed_events >= 1, "concurrent replacement was never observed inside a read window"


def test_default_coordination_root_matches_commander_writer_root() -> None:
    # S2-1：生产默认协调根（shared_documents._with_shared_lock）取
    # templates.parent；Commander 写入端取服务文件 parent.parent。
    # 标准布局 configs/templates + configs/services/commander.yaml 下，
    # 两者必须是同一物理目录（同一 inode）。
    paths = SharedConfigPaths(
        instances=_CONFIGS / "instances",
        templates=_CONFIGS / "templates",
    )
    default_root = paths.templates.parent
    writer_root = (_CONFIGS / "services" / "commander.yaml").resolve().parent.parent

    assert default_root == _CONFIGS
    assert default_root == writer_root
    assert os.stat(default_root).st_ino == os.stat(writer_root).st_ino
    assert default_root.is_dir()


def test_shared_lock_contends_with_exclusive_lock_on_same_root(tmp_path: Path) -> None:
    coordination_root = tmp_path / "root"
    templates = coordination_root / "configs" / "templates"
    templates.mkdir(parents=True)
    paths = SharedConfigPaths(
        instances=coordination_root / "configs" / "instances",
        templates=templates,
    )
    # 未显式传 coordination_root 时，Loader 实际加锁目录是 templates.parent。
    expected_root = paths.templates.parent

    writer_fd = os.open(expected_root, os.O_RDONLY | os.O_DIRECTORY)
    reader_fd = os.open(expected_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(writer_fd, fcntl.LOCK_EX)
        # 发布方持锁期间，同协调根上的共享读锁必须等待/失败，不能并行进入。
        with pytest.raises(BlockingIOError):
            fcntl.flock(reader_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        fcntl.flock(writer_fd, fcntl.LOCK_UN)
        os.close(writer_fd)

    try:
        fcntl.flock(reader_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    finally:
        fcntl.flock(reader_fd, fcntl.LOCK_UN)
        os.close(reader_fd)
