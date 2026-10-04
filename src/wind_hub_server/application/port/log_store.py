"""Server Admin Logs 使用的结构化日志查询端口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class LogEntry(BaseModel):
    """结构化日志记录。"""

    timestamp: datetime
    level: str
    source: str
    object: str
    message: str


class LogStorePort(Protocol):
    """LogQueryService 依赖的查询能力。"""

    def query(
        self,
        *,
        level: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
    ) -> list[LogEntry]: ...

    def sources(self) -> list[str]: ...
