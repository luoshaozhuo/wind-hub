"""Replay history source Port; Application cannot depend on CSV/SQLite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from data_generator.application.replay import ReplayRecord


class HistoryReaderPort(Protocol):
    def iter_records(
        self,
        source: Path,
        start: datetime,
        end: datetime,
        *,
        read_batch_size: int,
        max_file_bytes: int,
        max_records: int,
        point_types: Mapping[tuple[str, str], str],
    ) -> AsyncIterator[ReplayRecord]: ...
