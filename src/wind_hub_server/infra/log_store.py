"""进程内结构化日志缓冲。"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime

from wind_hub_server.application.port.log_store import LogEntry


class _BufferHandler(logging.Handler):
    """把标准 logging 记录投递到 LogStore。"""

    def __init__(self, store: LogStore) -> None:
        super().__init__()
        self._store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._store.append_record(record)
        except Exception:
            self.handleError(record)


class LogStore:
    """线程安全的有界进程日志缓冲。"""

    def __init__(self, capacity: int = 2000) -> None:
        if capacity <= 0:
            raise ValueError("log capacity must be greater than 0")
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._handler = _BufferHandler(self)
        self._installed = False

    def install(self) -> None:
        """挂到 root logger；幂等。"""
        with self._lock:
            if self._installed:
                return
            logging.getLogger().addHandler(self._handler)
            self._installed = True

    def uninstall(self) -> None:
        """从 root logger 卸载；幂等。"""
        with self._lock:
            if not self._installed:
                return
            logging.getLogger().removeHandler(self._handler)
            self._installed = False

    def append_record(self, record: logging.LogRecord) -> None:
        """把 LogRecord 规范化后入队。"""
        level = "WARN" if record.levelno == logging.WARNING else record.levelname
        object_name = str(
            getattr(record, "object", None)
            or getattr(record, "device_id", None)
            or getattr(record, "sink_name", None)
            or "—"
        )
        entry = LogEntry(
            timestamp=datetime.fromtimestamp(record.created, tz=UTC),
            level=level,
            source=self._source(record.name),
            object=object_name,
            message=record.getMessage(),
        )
        with self._lock:
            self._entries.appendleft(entry)

    @staticmethod
    def _source(logger_name: str) -> str:
        """把模块 logger 归并为前端可筛选的稳定来源。"""
        mapping = (
            (".protocol.ads", "ads"),
            (".protocol.modbus", "modbus"),
            (".protocol.iec104", "iec104"),
            (".sink.", "sink"),
            (".runtime", "runtime"),
            (".config", "config"),
            (".task", "task"),
        )
        for marker, source in mapping:
            if marker in logger_name:
                return source
        if logger_name.startswith("wind_hub_server"):
            return "server"
        return logger_name.rsplit(".", 1)[-1]

    def query(
        self,
        *,
        level: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
    ) -> list[LogEntry]:
        """按级别/来源/关键字过滤，保持最新在前。"""
        level_norm = level.upper() if level else None
        keyword_norm = keyword.lower().strip() if keyword else None
        with self._lock:
            rows = [entry.model_copy(deep=True) for entry in self._entries]
        if level_norm and level_norm != "ALL":
            rows = [row for row in rows if row.level == level_norm]
        if source and source != "All":
            rows = [row for row in rows if row.source == source]
        if keyword_norm:
            rows = [
                row
                for row in rows
                if keyword_norm
                in f"{row.source} {row.object} {row.message}".lower()
            ]
        return rows

    def sources(self) -> list[str]:
        """返回当前缓冲中的来源集合。"""
        with self._lock:
            return sorted({entry.source for entry in self._entries})
