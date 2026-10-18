"""CSV FileSink：标准库写入、按 UTC 日界/容量轮转、有限归档保留。"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import timedelta
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from core.application.csv_sink_record import FileSegment, iter_csv_rows, should_rotate
from core.application.file_sink_segments import (
    is_owned_segment,
    next_segment_sequence,
    segment_filename,
)
from core.application.protocol_contract import ConnectionHealth
from core.application.sink_error import SinkError
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import FileSinkConnection


class FileSink:
    """一份活动 CSV 文件及其归档；所有状态转换串行化。"""

    def __init__(self, config: Sink) -> None:
        cfg = FileSinkConnection.model_validate(dict(config.connection))
        if not isinstance(cfg, FileSinkConnection):
            raise ValueError("FileSink requires FileSinkConnection")
        path = Path(cfg.path)
        self._path = path / "telemetry.csv" if path.suffix.lower() != ".csv" else path
        self._policy = FileSinkConnection(
            path=str(self._path),
            max_size_mb=cfg.max_size_mb,
            max_files=cfg.max_files,
        )
        self._file: BinaryIO | None = None
        self._segment: FileSegment | None = None
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._healthy = False
        self._error: str | None = "not opened"
        self._task_root = Path(cfg.path)
        self._last_retention_check: str | None = None

    def _archives(self) -> list[Path]:
        return sorted(
            (
                p
                for p in self._path.parent.iterdir()
                if p.is_file() and is_owned_segment(p.name, base=self._path.name)
            ),
            key=lambda p: p.name,
        )

    def _open(self) -> None:
        if self._file is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        archives = self._archives()
        self._sequence = next_segment_sequence([p.name for p in archives], base=self._path.name)
        existing_size = self._path.stat().st_size if self._path.exists() else 0
        expected_header = next(iter_csv_rows([], include_header=True))
        if existing_size:
            with self._path.open("rb") as existing:
                old_header = existing.readline()
            if old_header != expected_header:
                # CSV 字段变化时不得把新行追加到旧表头下。
                opened_at = datetime.fromtimestamp(self._path.stat().st_mtime, UTC)
                while True:
                    name = segment_filename(
                        self._path.name,
                        opened_at=opened_at,
                        sequence=self._sequence,
                    )
                    self._sequence += 1
                    archive = self._path.with_name(name)
                    if not archive.exists():
                        break
                self._path.rename(archive)
                existing_size = 0
        now = (
            datetime.fromtimestamp(self._path.stat().st_mtime, UTC)
            if existing_size
            else datetime.now(UTC)
        )
        file = self._path.open("ab")
        try:
            if existing_size == 0:
                header = next(iter_csv_rows([], include_header=True))
                file.write(header)
                existing_size = len(header)
        except BaseException:
            file.close()
            raise
        self._file = file
        header_length = len(next(iter_csv_rows([], include_header=True)))
        self._segment = FileSegment(now, existing_size, existing_size > header_length)

    def _rotate(self) -> None:
        assert self._file is not None
        self._file.flush()
        self._file.close()
        self._file = None
        while True:
            filename = segment_filename(
                self._path.name,
                opened_at=datetime.now(UTC),
                sequence=self._sequence,
            )
            self._sequence += 1
            target = self._path.with_name(filename)
            if not target.exists():
                break
        self._path.rename(target)
        self._open()
        archives = self._archives()
        for stale in archives[: max(0, len(archives) - self._policy.max_files)]:
            stale.unlink()

    def _write(self, batch: list[PointValue]) -> None:
        if self._file is None or self._segment is None:
            raise SinkError("FileSink must be opened before write")
        for row in iter_csv_rows(batch):
            now = datetime.now(UTC)
            if should_rotate(self._policy, self._segment, next_row_bytes=len(row), now=now):
                self._rotate()
            assert self._file is not None and self._segment is not None
            self._file.write(row)
            self._segment = FileSegment(
                self._segment.opened_at,
                self._segment.byte_size + len(row),
                True,
            )
        self._file.flush()

    @staticmethod
    async def _blocking(operation: Callable[[], None]) -> None:
        """等待线程完成后才允许释放文件状态锁，即使调用者已取消。"""
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    async def open(self) -> None:
        async with self._lock:
            try:
                await self._blocking(self._open)
            except Exception as exc:
                self._healthy, self._error = False, str(exc)
                raise SinkError(f"CSV open failed: {exc}") from exc
            self._healthy, self._error = True, None

    async def write(self, batch: list[PointValue]) -> None:
        if not batch:
            return
        async with self._lock:
            try:
                await self._blocking(lambda: self._write(batch))
            except Exception as exc:
                self._healthy, self._error = False, str(exc)
                raise SinkError(f"CSV write failed: {exc}") from exc
            self._healthy, self._error = True, None

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
            raise SinkError(f"unsafe archive directory component: {value!r}")
        return value

    def _write_task(self, task_id: str, batch: list[PointValue]) -> None:
        """按 Task/设备/年月/采样日期归档，多个迟到日期也不混写。"""
        task = self._safe_segment(task_id)
        by_file: dict[tuple[str, str], list[PointValue]] = {}
        for point in batch:
            device = self._safe_segment(point.device_id)
            day = point.timestamp.astimezone(UTC).strftime("%Y%m%d")
            by_file.setdefault((device, day), []).append(point)
        header = next(iter_csv_rows([], include_header=True))
        max_bytes = self._policy.max_size_bytes
        for (device, day), values in by_file.items():
            directory = self._task_root / task / device / day[:4] / day[4:6]
            directory.mkdir(parents=True, exist_ok=True)
            index = 0
            for row in iter_csv_rows(values):
                while True:
                    target = directory / f"{day}_{index:03d}.csv"
                    exists = target.exists()
                    size = target.stat().st_size if exists else 0
                    if exists and size:
                        with target.open("rb") as current:
                            if current.readline() != header:
                                raise SinkError(f"CSV schema mismatch: {target}")
                    if size and size + len(row) > max_bytes:
                        index += 1
                        continue
                    with target.open("ab") as out:
                        if not size:
                            out.write(header)
                        out.write(row)
                    break
        now_day = datetime.now(UTC).strftime("%Y%m%d")
        if self._last_retention_check != now_day:
            self._last_retention_check = now_day
            cutoff = (
                datetime.now(UTC).date() - timedelta(days=self._policy.retention_days - 1)
            ).strftime("%Y%m%d")
            for target in self._task_root.glob("*/*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.csv"):
                stem = target.stem
                date, sep, number = stem.partition("_")
                if sep and len(date) == 8 and date.isdigit() and number.isdigit() and date < cutoff:
                    target.unlink(missing_ok=True)

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        """Task 归档入口；异步调用与写入/清理严格串行。"""
        if not batch:
            return
        async with self._lock:
            try:
                await self._blocking(lambda: self._write_task(task_id, batch))
            except Exception as exc:
                self._healthy, self._error = False, str(exc)
                raise SinkError(f"CSV task write failed: {exc}") from exc
            self._healthy, self._error = True, None

    async def flush(self) -> None:
        async with self._lock:
            if self._file is not None:
                try:
                    await self._blocking(self._file.flush)
                except Exception as exc:
                    self._healthy, self._error = False, str(exc)
                    raise SinkError(f"CSV flush failed: {exc}") from exc

    async def close(self) -> None:
        async with self._lock:
            if self._file is not None:
                file, self._file = self._file, None
                await self._blocking(file.close)
            self._segment = None
            self._healthy, self._error = False, "closed"

    async def health(self) -> ConnectionHealth:
        """检查活动文件与目标目录实际可写性，不写入业务记录。"""
        async with self._lock:
            if self._file is None or self._file.closed:
                return ConnectionHealth(healthy=False, message=self._error or "not opened")

            def _probe() -> None:
                # 打开现有文件的 append fd 验证权限/文件系统状态，不写字节；
                # 同时要求父目录仍可写，以保证后续轮转 rename/create 可执行。
                fd = os.open(self._path, os.O_WRONLY | os.O_APPEND)
                os.close(fd)
                if not os.access(self._path.parent, os.W_OK):
                    raise PermissionError(f"directory is not writable: {self._path.parent}")

            try:
                await self._blocking(_probe)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._healthy, self._error = False, str(exc)
                return ConnectionHealth(healthy=False, message=self._error)

            self._healthy, self._error = True, None
            return ConnectionHealth(healthy=True)
