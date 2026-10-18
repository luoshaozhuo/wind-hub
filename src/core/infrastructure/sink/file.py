"""CSV File Sink：按 Task/Device/UTC 年月日分目录、容量轮转与期限保留。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.application.csv_sink_record import iter_csv_rows
from core.application.protocol_contract import ConnectionHealth
from core.application.sink_error import SinkError
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import FileSinkConnection

logger = logging.getLogger(__name__)

_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")


class FileSink:
    """单进程串行写入，无跨批次文件句柄；按 Task/Device 独立归档。"""

    #: 诊断写入也必须携带 Task 身份（裸 write 拒绝），调用方经
    #: write_task("_diagnostic", ...) 落到独立诊断归档，不污染业务目录。
    requires_task_context: bool = True

    def __init__(self, config: Sink) -> None:
        self._policy = FileSinkConnection.model_validate(dict(config.connection))
        self._root = Path(self._policy.path)
        self._lock = asyncio.Lock()
        self._opened = False
        self._error: str | None = "not opened"
        self._last_cleanup = 0.0

    @staticmethod
    def _component(value: str) -> str:
        if not isinstance(value, str) or not _COMPONENT.fullmatch(value) or value in {".", ".."}:
            raise SinkError(f"unsafe archive path component: {value!r}")
        return value

    @staticmethod
    async def _blocking(operation: Callable[[], None]) -> None:
        # 文件操作开始后必须收敛，取消也不能提前释放锁。
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                raise

    def _within_root(self, path: Path) -> bool:
        """解析后的真实路径必须仍在归档根内（阻断符号链接逃逸）。"""
        root = self._root.resolve(strict=False)
        resolved = path.resolve(strict=False)
        return resolved == root or root in resolved.parents

    def _open_segment(self, path: Path, flags: int) -> int:
        """O_NOFOLLOW 打开分段：已存在的符号链接一律拒绝，不跟随写入。"""
        return os.open(path, flags, 0o640)

    def _append_rows(self, directory: Path, day: str, values: list[PointValue]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        if not self._within_root(directory):
            raise SinkError(f"archive directory escapes root via symlink: {directory}")
        header = next(iter_csv_rows([], include_header=True))
        max_size = self._policy.max_size_bytes
        index = 0
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        for row in iter_csv_rows(values):
            while True:
                path = directory / f"{day}_{index:03d}.csv"
                size = path.stat().st_size if path.exists() else 0
                if size:
                    if path.is_symlink():
                        raise SinkError(f"refusing to write through symlink: {path}")
                    fd = self._open_segment(path, os.O_RDONLY | nofollow)
                    with os.fdopen(fd, "rb") as existing:
                        if existing.readline() != header:
                            raise SinkError(f"CSV header does not match: {path}")
                # 超过容量后写入下一个分段。单行超过上限时仍允许独占分段。
                if size and size + len(row) > max_size:
                    index += 1
                    continue
                write_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | nofollow
                with os.fdopen(self._open_segment(path, write_flags), "ab") as file:
                    if size == 0:
                        file.write(header)
                    file.write(row)
                break

    def _cleanup(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_cleanup < 3600:
            return
        cutoff = (datetime.now(UTC).date() - timedelta(days=self._policy.retention_days - 1))
        # 仅清理本 Sink 所拥有的 Task/Device/年/月/日期索引 CSV；
        # 符号链接与解析后逃逸出根目录的条目一律不删（防共享根/链接投毒）。
        for file in self._root.glob("*/*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.csv"):
            if file.is_symlink() or not self._within_root(file):
                logger.warning("skip cleanup of path escaping archive root: %s", file)
                continue
            day, sep, index = file.stem.partition("_")
            if not sep or len(day) != 8 or not day.isdigit() or not index.isdigit():
                continue
            try:
                file_day = datetime.strptime(day, "%Y%m%d").date()
            except ValueError:
                continue
            if file_day < cutoff:
                file.unlink(missing_ok=True)
        self._last_cleanup = now

    def _write_task(self, task_id: str, batch: list[PointValue]) -> None:
        task = self._component(task_id)
        by_file: dict[tuple[str, str], list[PointValue]] = {}
        for point in batch:
            device = self._component(point.device_id)
            day = point.timestamp.astimezone(UTC).strftime("%Y%m%d")
            by_file.setdefault((device, day), []).append(point)
        for (device, day), values in by_file.items():
            directory = self._root / task / device / day[:4] / day[4:6]
            self._append_rows(directory, day, values)
        self._cleanup()

    async def open(self) -> None:
        async with self._lock:
            try:
                await self._blocking(lambda: self._root.mkdir(parents=True, exist_ok=True))
                self._opened = True
                self._error = None
                await self._blocking(lambda: self._cleanup(force=True))
            except Exception as exc:
                self._opened = False
                self._error = str(exc)
                raise SinkError(f"FileSink open failed: {exc}") from exc

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        if not batch:
            return
        async with self._lock:
            if not self._opened:
                raise SinkError("FileSink must be opened before writing")
            try:
                await self._blocking(lambda: self._write_task(task_id, batch))
                self._error = None
            except Exception as exc:
                self._error = str(exc)
                raise SinkError(f"FileSink write failed: {exc}") from exc

    async def write(self, batch: list[PointValue]) -> None:
        """禁止无 Task 身份的写入，防止数据落入错误目录。"""
        if batch:
            raise SinkError("FileSink requires task_id: use write_task()")

    async def flush(self) -> None:
        """每批写入都关闭文件，无待刷新的内部缓冲。"""

    async def close(self) -> None:
        async with self._lock:
            self._opened = False
            self._error = "closed"

    async def health(self) -> ConnectionHealth:
        async with self._lock:
            if not self._opened:
                return ConnectionHealth(healthy=False, message=self._error or "not opened")
            try:
                def probe() -> None:
                    if not self._root.is_dir() or not os.access(self._root, os.W_OK):
                        raise PermissionError(f"archive directory is not writable: {self._root}")
                    self._cleanup()

                await self._blocking(probe)
            except Exception as exc:
                self._error = str(exc)
                return ConnectionHealth(healthy=False, message=self._error)
            self._error = None
            return ConnectionHealth(healthy=True)
