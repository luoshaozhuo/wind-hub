"""File sink 的异步滚动文件写入器。

基于 ``aiofiles`` 把文件生命周期（open / append / flush / rollover / close）
全部移出事件循环线程，``FileSink`` 只负责序列化与健康状态映射。

并发约定：本类**不持有自己的锁**。所有 public 方法的调用方（``FileSink``）
必须已通过同一把 ``asyncio.Lock`` 串行化；``aiofiles`` 的 ``await`` 会让出
事件循环，锁保证 write / flush / rollover / close 不会交叉破坏文件状态机。
内部 ``*_locked`` 助手表示"调用方已持锁"。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from wind_hub_collector.adapter.outbound.sink.file.compression import (
    Compressor,
    NoCompressor,
)

if TYPE_CHECKING:
    from aiofiles.threadpool.binary import AsyncBufferedIOBase

    from wind_hub_collector.adapter.outbound.sink.file.rotation import RotationPolicy

logger = logging.getLogger(__name__)


class AsyncRotatingFileWriter:
    """单活动文件的异步写入器：缓冲、刷盘、滚动与后台压缩调度。

    Args:
        path: 活动文件路径，父目录不存在时自动创建。
        rotation: 滚动策略（大小/时间/组合/不滚动）。
        compressor: 滚动归档的压缩器；``NoCompressor`` 时不调度后台压缩。
        header: 新建空文件时需要写入的表头字节（CSV），``None`` 表示无表头。
        buffer_size: 缓冲行数达到该值即刷盘。
        flush_interval: 距上次刷盘超过该秒数即刷盘（append 时惰性检查）；
            ``<= 0`` 表示禁用时间触发。
    """

    def __init__(
        self,
        path: Path,
        rotation: RotationPolicy,
        compressor: Compressor,
        header: bytes | None,
        buffer_size: int,
        flush_interval: float,
    ) -> None:
        self._path = path
        self._rotation = rotation
        self._compressor = compressor
        self._header = header
        self._buffer_size = buffer_size
        self._flush_interval = flush_interval
        self._file: AsyncBufferedIOBase | None = None
        self._buffer = bytearray()
        self._buffer_lines = 0
        self._current_size = 0
        self._opened_at = 0.0
        self._last_flush = 0.0
        self._compress_tasks: set[asyncio.Task[None]] = set()

    @property
    def is_open(self) -> bool:
        """活动文件句柄是否已打开。"""
        return self._file is not None

    # -- 生命周期（调用方须已持锁） ----------------------------------------

    async def open(self) -> None:
        """创建父目录并以追加模式异步打开文件；空文件补写表头。

        Raises:
            OSError: 路径不可写（父目录无法创建、权限不足等）。
        """
        await aiofiles.os.makedirs(self._path.parent, exist_ok=True)
        try:
            stat_result = await aiofiles.os.stat(self._path)
            self._current_size = stat_result.st_size
        except FileNotFoundError:
            self._current_size = 0
        self._file = await aiofiles.open(self._path, "ab")
        if self._header is not None and self._current_size == 0:
            await self._file.write(self._header)
            self._current_size += len(self._header)
        self._opened_at = time.monotonic()
        self._last_flush = time.monotonic()

    async def close(self) -> None:
        """冲刷缓冲并关闭文件；幂等，已关闭时无操作。

        Raises:
            OSError: 冲刷或关闭失败（文件句柄仍被释放）。
        """
        if self._file is None:
            return
        try:
            await self._flush_locked()
        except OSError:
            file, self._file = self._file, None
            await file.close()
            raise
        file, self._file = self._file, None
        await file.close()

    async def append(self, payload: bytes, lines: int) -> None:
        """把已序列化的字节追加进缓冲，按阈值/间隔刷盘，随后检查滚动。

        Args:
            payload: 一批完整行（含行尾换行）的字节。
            lines: ``payload`` 包含的行数，用于 ``buffer_size`` 判定。

        Raises:
            OSError: 刷盘或滚动失败。
        """
        self._buffer += payload
        self._buffer_lines += lines
        if self._should_flush():
            await self._flush_locked()
        if self._rotation.should_rotate(
            self._current_size, time.monotonic() - self._opened_at
        ):
            await self._rollover_locked()

    async def flush(self) -> None:
        """立即把缓冲写入文件并冲刷到底层；未打开时无操作。

        Raises:
            OSError: 刷盘失败。
        """
        if self._file is None:
            return
        await self._flush_locked()

    # -- 内部：刷盘 / 滚动（*_locked：调用方已持锁） ------------------------

    def _should_flush(self) -> bool:
        """缓冲行数达到阈值，或距上次刷盘超过 ``flush_interval`` 秒。"""
        if self._buffer_lines >= self._buffer_size:
            return True
        if self._flush_interval > 0:
            return time.monotonic() - self._last_flush >= self._flush_interval
        return False

    async def _flush_locked(self) -> None:
        """把缓冲写入文件并冲刷；调用方须已持锁且文件已打开。"""
        if not self._buffer:
            return
        assert self._file is not None
        payload = bytes(self._buffer)
        await self._file.write(payload)
        await self._file.flush()
        self._current_size += len(payload)
        self._buffer.clear()
        self._buffer_lines = 0
        self._last_flush = time.monotonic()

    async def _rollover_locked(self) -> None:
        """冲刷并关闭当前文件、改名归档、调度压缩、重开新文件。

        调用方须已持锁且文件已打开。rename 完成前不打开新文件，
        避免活动路径与归档路径状态交叉。
        """
        await self._flush_locked()
        assert self._file is not None
        file, self._file = self._file, None
        await file.close()
        rolled = self._rolled_path()
        await aiofiles.os.rename(self._path, rolled)
        self._schedule_compression(rolled)
        await self.open()

    def _rolled_path(self) -> Path:
        """滚动文件名 ``{base}.{suffix}.{ext}``，后缀由滚动策略生成。"""
        ts = self._rotation.rotation_suffix(datetime.now(UTC))
        return self._path.with_name(f"{self._path.stem}.{ts}{self._path.suffix}")

    # -- 内部：后台压缩 ---------------------------------------------------

    def _schedule_compression(self, path: Path) -> None:
        """把旧分片交给后台 task 压缩，不阻塞写入路径。"""
        if isinstance(self._compressor, NoCompressor):
            return
        task = asyncio.create_task(self._compress_async(path))
        self._compress_tasks.add(task)
        task.add_done_callback(self._compress_tasks.discard)

    async def _compress_async(self, path: Path) -> None:
        """在独立 task 里压缩；压缩含阻塞 I/O 与 CPU 工作，放到线程池。"""
        try:
            await asyncio.to_thread(self._compressor.compress, path)
            logger.info("compressed archived file %s", path)
        except Exception as exc:
            # 压缩是采集主流程之外的 best-effort 增强：失败只记录日志，
            # 不得让后台 task 的异常中断滚动/写入。
            logger.warning("compression failed for %s: %s", path, exc)

    def detach_compression_tasks(self) -> list[asyncio.Task[None]]:
        """取走当前压缩 task 快照；调用方在锁外等待，避免持锁等后台任务。"""
        tasks = list(self._compress_tasks)
        self._compress_tasks.clear()
        return tasks
