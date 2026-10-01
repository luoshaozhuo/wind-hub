"""进程内结构化事件日志。"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

LogLevel = Literal["INFO", "WARN", "ERROR"]


class EventLogEntry(BaseModel):
    """Admin 日志查询使用的结构化事件。"""

    time: datetime
    level: LogLevel
    source: str
    object: str
    message: str


class EventLogStore:
    """有界、线程安全的进程内事件日志。"""

    def __init__(self, max_entries: int = 5000) -> None:
        self._items: deque[EventLogEntry] = deque(maxlen=max_entries)
        self._lock = threading.RLock()

    def append(self, level: LogLevel, source: str, obj: str, message: str) -> None:
        with self._lock:
            self._items.appendleft(
                EventLogEntry(
                    time=datetime.now(UTC),
                    level=level,
                    source=source,
                    object=obj,
                    message=message,
                )
            )

    def query(
        self,
        *,
        level: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
    ) -> list[EventLogEntry]:
        query = (keyword or "").strip().lower()
        with self._lock:
            rows = list(self._items)
        return [
            row
            for row in rows
            if (not level or level == "All" or row.level == level)
            and (not source or source == "All" or row.source == source)
            and (
                not query
                or query in f"{row.source} {row.object} {row.message}".lower()
            )
        ]
