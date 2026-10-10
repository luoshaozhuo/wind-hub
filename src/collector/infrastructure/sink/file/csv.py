"""CSV FileSink：标准库写入、按 UTC 日界/容量轮转、有限归档保留。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Sequence

from collector.application.errors import SinkError
from core.application.csv_sink_record import FileSegment, iter_csv_rows, should_rotate
from core.application.file_sink_segments import (
    is_owned_segment,
    next_segment_sequence,
    segment_filename,
)
from core.application.protocol_contract import ConnectionHealth
from core.application.sink_config import FileSinkConnection, ResolvedSinkConfig
from core.application.sink_contract import FileSinkConnection as FilePolicy
from core.domain.point_value import PointValue


class FileSink:
    """一份活动 CSV 文件及其归档；所有状态转换串行化。"""

    def __init__(self, config: ResolvedSinkConfig) -> None:
        cfg = config.connection
        if not isinstance(cfg, FileSinkConnection):
            raise ValueError("FileSink requires FileSinkConnection")
        path = Path(cfg.path)
        self._path = path / "telemetry.csv" if path.suffix.lower() != ".csv" else path
        self._policy = FilePolicy(
            path=str(self._path),
            max_size_mb=cfg.max_size_mb,
            max_files=cfg.max_files,
            buffer_size=cfg.buffer_size,
            flush_interval=cfg.flush_interval,
        )
        self._file: BinaryIO | None = None
        self._segment: FileSegment | None = None
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._healthy = False
        self._error: str | None = "not opened"

    def _archives(self) -> list[Path]:
        return sorted(
            (p for p in self._path.parent.iterdir()
             if p.is_file() and is_owned_segment(p.name, base=self._path.name)),
            key=lambda p: p.name,
        )

    def _open(self) -> None:
        if self._file is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        archives = self._archives()
        self._sequence = next_segment_sequence(
            [p.name for p in archives], base=self._path.name
        )
        existing_size = self._path.stat().st_size if self._path.exists() else 0
        now = (datetime.fromtimestamp(self._path.stat().st_mtime, UTC)
               if existing_size else datetime.now(UTC))
        self._file = self._path.open("ab")
        if existing_size == 0:
            header = next(iter_csv_rows([], include_header=True))
            self._file.write(header)
            existing_size = len(header)
        header_length = len(next(iter_csv_rows([], include_header=True)))
        self._segment = FileSegment(now, existing_size, existing_size > header_length)

    def _rotate(self) -> None:
        assert self._file is not None
        self._file.flush()
        self._file.close()
        self._file = None
        while True:
            filename = segment_filename(
                self._path.name, opened_at=datetime.now(UTC),
                sequence=self._sequence,
            )
            self._sequence += 1
            target = self._path.with_name(filename)
            if not target.exists():
                break
        self._path.rename(target)
        self._open()
        archives = self._archives()
        for stale in archives[:max(0, len(archives) - self._policy.max_files)]:
            stale.unlink()

    def _write(self, batch: Sequence[PointValue]) -> None:
        if self._file is None or self._segment is None:
            raise SinkError("FileSink must be opened before write")
        for row in iter_csv_rows(batch):
            now = datetime.now(UTC)
            if should_rotate(
                self._policy, self._segment, next_row_bytes=len(row), now=now
            ):
                self._rotate()
            assert self._file is not None and self._segment is not None
            self._file.write(row)
            self._segment = FileSegment(
                self._segment.opened_at,
                self._segment.byte_size + len(row),
                True,
            )
        self._file.flush()

    async def open(self) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(self._open)
            except Exception as exc:
                self._healthy, self._error = False, str(exc)
                raise SinkError(f"CSV open failed: {exc}") from exc
            self._healthy, self._error = True, None

    async def write(self, batch: Sequence[PointValue]) -> None:
        if not batch:
            return
        async with self._lock:
            try:
                await asyncio.to_thread(self._write, batch)
            except Exception as exc:
                self._healthy, self._error = False, str(exc)
                raise SinkError(f"CSV write failed: {exc}") from exc
            self._healthy, self._error = True, None

    async def flush(self) -> None:
        async with self._lock:
            if self._file is not None:
                try:
                    await asyncio.to_thread(self._file.flush)
                except Exception as exc:
                    self._healthy, self._error = False, str(exc)
                    raise SinkError(f"CSV flush failed: {exc}") from exc

    async def close(self) -> None:
        async with self._lock:
            if self._file is not None:
                file, self._file = self._file, None
                await asyncio.to_thread(file.close)
            self._segment = None
            self._healthy, self._error = False, "closed"

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=self._healthy, message=self._error)
