"""进程日志配置 log_setup.py 契约。

- UTC 时间戳与 request-id 注入；
- 文件按 UTC 日界与容量轮转；
- 过期日志只在日界清理，活动文件不删除；
- process_name 白名单；handler 关闭刷新。
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.application.settings import LogSettings
from core.infrastructure.diagnostic.correlation import bind_request_id
from core.infrastructure.diagnostic.log_setup import (
    _DailySizeFileHandler,
    configure_logging,
    configure_process_logging,
)


def _settings(tmp_path: Path, **overrides) -> LogSettings:
    values = {"path": str(tmp_path), "console": False, "max_size_mb": 0.0001, "retention_days": 2}
    values.update(overrides)
    return LogSettings(**values)


def test_configure_logging_uses_utc_converter_and_request_filter() -> None:
    configure_logging()
    root = logging.getLogger()
    assert root.handlers
    handler = root.handlers[0]
    assert handler.formatter is not None
    assert handler.formatter.converter is time.gmtime


def test_process_logging_writes_file_with_request_id(tmp_path: Path) -> None:
    configure_process_logging(_settings(tmp_path), process_name="collector")
    with bind_request_id("req-123"):
        logging.getLogger("test.diag").warning("hello diagnostic")
    logging.getLogger("test.diag").warning("without context")
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_dir = tmp_path / "collector"
    content = "".join(f.read_text(encoding="utf-8") for f in log_dir.glob("*.log"))
    assert "req-123" in content
    assert "hello diagnostic" in content
    assert "[-]" in content  # 无上下文时 request-id 回退为 -


def test_invalid_process_name_rejected(tmp_path: Path) -> None:
    for bad in ["", "a b", "a/b", "中文"]:
        with pytest.raises(ValueError):
            configure_process_logging(_settings(tmp_path), process_name=bad)


def test_size_rotation_creates_indexed_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_size_mb=0.0001)  # ~105 字节上限
    handler = _DailySizeFileHandler(settings, "commander")
    logger = logging.getLogger("test.size-rotation")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    for i in range(10):
        logger.warning("x" * 80 + str(i))
    handler.close()

    files = sorted(p.name for p in (tmp_path / "commander").glob("*.log"))
    assert len(files) >= 2
    assert all(re.fullmatch(r"\d{8}_\d{3}\.log", name) for name in files)


def test_day_rollover_opens_new_dated_file(tmp_path: Path) -> None:
    handler = _DailySizeFileHandler(_settings(tmp_path), "seahorse")
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="first day",
        args=(),
        exc_info=None,
    )
    record.created = datetime(2030, 1, 1, 0, 0, tzinfo=UTC).timestamp()
    handler.emit(record)

    record2 = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="second day",
        args=(),
        exc_info=None,
    )
    record2.created = datetime(2030, 1, 2, 0, 0, tzinfo=UTC).timestamp()
    handler.emit(record2)
    handler.close()

    names = sorted(p.name for p in (tmp_path / "seahorse").glob("*.log"))
    assert names == ["20300101_000.log", "20300102_000.log"]


def test_retention_deletes_only_expired_files_on_day_boundary(tmp_path: Path) -> None:
    log_dir = tmp_path / "core"
    log_dir.mkdir(parents=True)
    old = log_dir / "20000101_000.log"
    recent = log_dir / "20990101_000.log"
    old.write_text("old\n")
    recent.write_text("keep\n")

    handler = _DailySizeFileHandler(_settings(tmp_path, retention_days=7), "core")
    today = datetime.now(UTC)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "trigger cleanup", (), None)
    record.created = today.timestamp()
    handler.emit(record)
    handler.close()

    assert not old.exists()
    assert recent.exists()


def test_handler_close_flushes_stream(tmp_path: Path) -> None:
    handler = _DailySizeFileHandler(_settings(tmp_path), "collector")
    stream = io.StringIO()
    handler._stream = stream  # type: ignore[assignment]
    handler.close()
    assert stream.closed
