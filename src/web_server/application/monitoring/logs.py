"""Admin Logs 查询服务。"""

from __future__ import annotations

from pydantic import BaseModel

from wind_hub_server.application.port.log_store import LogEntry, LogStorePort


class LogPage(BaseModel):
    items: list[LogEntry]
    page: int
    page_size: int
    total: int


class LogQueryService:
    """结构化日志筛选与分页。"""

    def __init__(self, store: LogStorePort) -> None:
        self._store = store

    def list_logs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        level: str | None = None,
        source: str | None = None,
        keyword: str | None = None,
    ) -> LogPage:
        rows = self._store.query(level=level, source=source, keyword=keyword)
        start = (page - 1) * page_size
        return LogPage(
            items=rows[start : start + page_size],
            page=page,
            page_size=page_size,
            total=len(rows),
        )

    def sources(self) -> list[str]:
        return self._store.sources()
