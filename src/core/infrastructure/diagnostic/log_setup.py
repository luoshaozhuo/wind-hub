"""进程入口统一日志配置：UTC 时间戳 + 级别 + logger 名。"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from core.application.settings import LogSettings
from core.infrastructure.diagnostic.correlation import RequestIdFilter

_FORMAT = "%(asctime)sZ %(levelname)s [%(request_id)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """配置根日志：时间戳一律 UTC（ISO-8601，秒级），便于跨进程对齐故障时序。"""
    formatter = logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


class _DailySizeFileHandler(logging.Handler):
    """UTC 日界与容量轮转；每个进程独占其子目录。"""

    def __init__(self, settings: LogSettings, process_name: str) -> None:
        super().__init__()
        self._root = Path(settings.path) / process_name
        self._root.mkdir(parents=True, exist_ok=True)
        self._limit = int(settings.max_size_mb * 1024 * 1024)
        self._retention_days = settings.retention_days
        self._date = ""
        self._index = 0
        self._stream: TextIO | None = None
        self._last_cleanup = ""

    def _open_for(self, day: str, size: int) -> None:
        if self._stream is not None:
            self._stream.close()
        if day != self._date:
            self._index = 0
        self._date = day
        while True:
            target = self._root / f"{day}_{self._index:03d}.log"
            if not target.exists():
                break
            current_size = target.stat().st_size
            if current_size == 0 or current_size + size <= self._limit:
                break
            self._index += 1
        self._stream = target.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            timestamp = datetime.fromtimestamp(record.created, UTC)
            day = timestamp.strftime("%Y%m%d")
            message = self.format(record) + "\n"
            size = len(message.encode("utf-8"))
            if self._stream is None or day != self._date:
                self._open_for(day, size)
            elif self._stream.tell() + size > self._limit:
                self._index += 1
                self._open_for(day, size)
            assert self._stream is not None
            self._stream.write(message)
            self._stream.flush()
            cutoff = (
                timestamp.date() - timedelta(days=self._retention_days - 1)
            ).strftime("%Y%m%d")
            # 清理仅在日界发生时执行；活动文件不参与删除。
            if self._last_cleanup != day:
                self._last_cleanup = day
                for file in self._root.glob("????????_*.log"):
                    stem = file.stem
                    date, separator, index = stem.partition("_")
                    if (
                        separator
                        and len(date) == 8
                        and date.isdigit()
                        and index.isdigit()
                        and date < cutoff
                    ):
                        file.unlink(missing_ok=True)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            super().close()


def configure_process_logging(config: LogSettings, *, process_name: str) -> None:
    """独占进程日志目录，配置控制台与日期/容量文件输出。"""
    if not process_name or not all(
        char.isascii() and (char.isalnum() or char in "-_") for char in process_name
    ):
        raise ValueError("invalid process_name")
    formatter = logging.Formatter(fmt=_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime
    handlers: list[logging.Handler] = []
    if config.console:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.addFilter(RequestIdFilter())
        handlers.append(console)
    file_handler = _DailySizeFileHandler(config, process_name)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RequestIdFilter())
    handlers.append(file_handler)
    logging.basicConfig(level=getattr(logging, config.level), handlers=handlers, force=True)
