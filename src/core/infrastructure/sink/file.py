"""CSV File Sink：按 Task/Device/UTC 年月日分目录、容量轮转与期限保留。"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

    @staticmethod
    def _open_segment(path: str, flags: int, *, dir_fd: int) -> int:
        """相对已验证目录描述符打开文件，杜绝路径分量替换导致的逃逸。"""
        try:
            return os.open(path, flags, 0o640, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SinkError(f"refusing to write through symlink: {path}") from exc
            raise

    @contextmanager
    def _directory_fd(self, directory: Path) -> Iterator[int]:
        """目录逐层使用 dir_fd + O_NOFOLLOW 打开，不依赖事前 stat 判定。"""
        root = self._root.absolute()
        try:
            parts = directory.absolute().relative_to(root).parts
        except ValueError as exc:
            raise SinkError(f"archive directory escapes root: {directory}") from exc
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        # 打开根的每个分量以拒绝归档根自身及其祖先的符号链接。
        components = root.parts
        current = os.open(components[0], flags)
        try:
            for part in (*components[1:], *parts):
                if part in {".", ".."}:
                    raise SinkError(f"unsafe archive directory: {directory}")
                try:
                    os.mkdir(part, dir_fd=current)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(part, flags, dir_fd=current)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        raise SinkError(
                            f"archive directory escapes root via symlink: {directory}"
                        ) from exc
                    raise
                os.close(current)
                current = next_fd
            yield current
        finally:
            os.close(current)

    def _append_rows(self, directory: Path, day: str, values: list[PointValue]) -> None:
        """每个分段仅打开一次，按记录边界轮转，写入前计算表头大小。"""
        header = next(iter_csv_rows([], include_header=True))
        max_size = self._policy.max_size_bytes
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        index = 0
        with self._directory_fd(directory) as dir_fd:
            file = None
            size = 0
            has_records = False
            pending = bytearray()

            def close_segment() -> None:
                nonlocal file
                if file is None:
                    return
                try:
                    if pending:
                        file.write(pending)
                    file.flush()
                    pending.clear()
                finally:
                    file.close()
                    file = None

            def open_segment() -> None:
                nonlocal file, size, has_records, index
                while True:
                    filename = f"{day}_{index:03d}.csv"
                    fd = self._open_segment(filename, flags, dir_fd=dir_fd)
                    try:
                        metadata = os.fstat(fd)
                        if not stat.S_ISREG(metadata.st_mode):
                            raise SinkError(f"CSV segment is not a regular file: {filename}")
                        candidate = os.fdopen(fd, "r+b")
                    except BaseException:
                        os.close(fd)
                        raise
                    try:
                        size = metadata.st_size
                        if size:
                            if candidate.readline() != header:
                                raise SinkError(f"CSV header does not match: {filename}")
                        else:
                            candidate.write(header)
                            candidate.flush()
                            size = len(header)
                        has_records = size > len(header)
                        if has_records and size >= max_size:
                            candidate.close()
                            index += 1
                            continue
                        file = candidate
                        return
                    except BaseException:
                        candidate.close()
                        raise

            try:
                open_segment()
                for row in iter_csv_rows(values):
                    if has_records and size + len(row) > max_size:
                        close_segment()
                        index += 1
                        open_segment()
                    pending.extend(row)
                    size += len(row)
                    has_records = True
                    if len(pending) >= 256 * 1024:
                        assert file is not None
                        file.write(pending)
                        file.flush()
                        pending.clear()
            finally:
                close_segment()

    def _cleanup(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_cleanup < 3600:
            return
        cutoff = datetime.now(UTC).date() - timedelta(days=self._policy.retention_days - 1)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)

        def child_dirs(parent_fd: int) -> Iterator[tuple[str, int]]:
            for name in os.listdir(parent_fd):
                if name in {".", ".."}:
                    continue
                try:
                    fd = os.open(name, flags, dir_fd=parent_fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENOENT}:
                        continue
                    raise
                yield name, fd

        # 仅在 Task/Device/YYYY/MM 四级归档目录中删除匹配日期的常规文件。
        # 所有目录均由 dir_fd 锚定，即使外部并发替换路径也不会逃逸根目录。
        with self._directory_fd(self._root) as root_fd:
            for task, task_fd in child_dirs(root_fd):
                try:
                    if not _COMPONENT.fullmatch(task) or task in {".", ".."}:
                        continue
                    for device, device_fd in child_dirs(task_fd):
                        try:
                            if not _COMPONENT.fullmatch(device) or device in {".", ".."}:
                                continue
                            for year, year_fd in child_dirs(device_fd):
                                try:
                                    if len(year) != 4 or not year.isdigit():
                                        continue
                                    for month, month_fd in child_dirs(year_fd):
                                        try:
                                            if len(month) != 2 or not month.isdigit():
                                                continue
                                            for name in os.listdir(month_fd):
                                                match = re.fullmatch(
                                                    r"([0-9]{8})_([0-9]+)\\.csv", name
                                                )
                                                if match is None:
                                                    continue
                                                day = match.group(1)
                                                if day[:4] != year or day[4:6] != month:
                                                    continue
                                                try:
                                                    file_day = datetime.strptime(
                                                        day, "%Y%m%d"
                                                    ).date()
                                                except ValueError:
                                                    continue
                                                if file_day >= cutoff:
                                                    continue
                                                try:
                                                    metadata = os.stat(
                                                        name, dir_fd=month_fd,
                                                        follow_symlinks=False,
                                                    )
                                                    if not stat.S_ISREG(metadata.st_mode):
                                                        continue
                                                    os.unlink(name, dir_fd=month_fd)
                                                except FileNotFoundError:
                                                    continue
                                        finally:
                                            os.close(month_fd)
                                finally:
                                    os.close(year_fd)
                        finally:
                            os.close(device_fd)
                finally:
                    os.close(task_fd)
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
