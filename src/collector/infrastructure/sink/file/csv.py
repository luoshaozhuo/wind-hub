"""本地文件输出 sink —— 追加写 CSV / JSONL，支持滚动策略与后台压缩。

实现 :class:`~collector.application.sink_port.SinkPort` 的真实文件走向：把一批
:class:`~collector.domain.point_value.PointValue` 序列化成 ``csv`` 或 ``jsonl``
行字节，交给 :class:`~.async_writer.AsyncRotatingFileWriter` 异步落盘；滚动判断
委托给 :class:`~.rotation.RotationPolicy`（由 ``connection`` 里的
``max_size_mb`` / ``max_age_hours`` 构建），``buffer_size`` / ``flush_interval``
控制刷盘时机，``close`` 时强制 flush 并在锁外等待后台压缩完成。

职责划分：

- ``FileSink``：SinkPort 契约、``PointValue`` → 行字节序列化、健康/错误映射，
  并用一把 ``asyncio.Lock`` 串行化 writer 的文件生命周期状态机
  （open / append / flush / rollover / close）。序列化是纯 CPU 操作，
  在拿锁之前完成。
- ``AsyncRotatingFileWriter``：基于 ``aiofiles`` 的异步文件 I/O、缓冲、
  刷盘、滚动、归档改名与后台压缩调度。

所有文件级参数从 ``FileSinkConnection`` 读取并在**构造时**校验；写文件失败
（磁盘满、权限不足等）抛 :class:`~collector.application.errors.SinkError`，
连续失败达到阈值后 ``health()`` 报告 unhealthy。压缩失败仅记录日志，不影响
采集主流程。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from pathlib import Path

from collector.application.errors import SinkError
from collector.application.sink_port import SinkPort
from collector.application.sinks import FileSinkConnection, SinkConfig
from collector.domain.point_value import PointValue
from collector.infrastructure.sink.file.async_writer import (
    AsyncRotatingFileWriter,
)
from collector.infrastructure.sink.file.compression import (
    Compressor,
    GzipCompressor,
    NoCompressor,
)
from collector.infrastructure.sink.file.rotation import build_rotation
from core.application import ConfigError, ConnectionHealth, PointScalar

logger = logging.getLogger(__name__)

# CSV 表头固定列序，与 `_serialize_row` 的 CSV 行一致。
_CSV_HEADER = ["device_id", "point_id", "value", "quality", "timestamp", "source"]

# 连续写/刷盘失败达到该次数后，sink 标记为 unhealthy（决策 8）。
_MAX_CONSECUTIVE_FAILURES = 5


class FileSink(SinkPort):
    """追加写本地文件的输出 sink。

    参数（``FileSinkConnection``）：

    - ``path``（必填）：目标文件路径，父目录不存在时自动创建。
    - ``format``：``csv`` 或 ``jsonl``，默认 ``jsonl``。
    - ``max_size_mb``：文件超过该大小（MB）即滚动，默认不滚动。
    - ``max_age_hours``：文件打开超过该时长（小时）即滚动，默认不滚动。
    - ``compress``：滚动后是否对旧分片 gzip 压缩，默认 ``False``。
    - ``compress_level``：gzip 压缩级别（1-9），默认 ``6``。
    - ``buffer_size``：缓冲行数达到该值即刷盘，默认 ``100``。
    - ``flush_interval``：距上次刷盘超过该秒数即刷盘（写时惰性检查），默认 ``1.0``。
    - ``write_header``：CSV 是否写表头，默认 ``True``（jsonl 忽略）。
    """

    def __init__(self, config: SinkConfig) -> None:
        connection = config.connection
        if not isinstance(connection, FileSinkConnection):
            raise ConfigError("FileSink requires FileSinkConnection")
        self._path = Path(connection.path)
        self._format = connection.format
        compressor: Compressor = (
            GzipCompressor(level=connection.compress_level)
            if connection.compress
            else NoCompressor()
        )
        header = (
            (",".join(_CSV_HEADER) + "\n").encode("utf-8")
            if connection.format == "csv" and connection.write_header
            else None
        )
        self._writer = AsyncRotatingFileWriter(
            self._path,
            rotation=build_rotation(connection.max_size_mb, connection.max_age_hours),
            compressor=compressor,
            header=header,
            buffer_size=connection.buffer_size,
            flush_interval=connection.flush_interval,
        )
        # 只保护 writer 的文件生命周期状态机一致性，不用于"让 I/O 变异步"。
        self._lock = asyncio.Lock()
        self._healthy = True
        self._error_message: str | None = None
        self._consecutive_failures = 0

    # -- SinkPort 契约 ----------------------------------------------------

    async def open(self) -> None:
        """创建父目录并打开文件（追加模式）；幂等，已打开时无操作。

        Raises:
            SinkError: 路径不可写（父目录无法创建、权限不足等）时抛出。
        """
        async with self._lock:
            if self._writer.is_open:
                return
            try:
                await self._writer.open()
            except OSError as exc:
                self._record_failure(f"open failed: {exc}")
                raise SinkError(f"FileSink open failed for {self._path}: {exc}") from exc

    async def close(self) -> None:
        """强制 flush 缓冲后关闭文件；幂等，已关闭时无操作。

        后台压缩 task 与活动文件状态无关：锁内只 flush/close 并取走 task
        快照，等待在锁外完成，避免持锁等待无关后台任务。

        Raises:
            SinkError: 冲刷或关闭文件失败时抛出（文件句柄仍被释放）。
        """
        async with self._lock:
            try:
                await self._writer.close()
            except OSError as exc:
                self._record_failure(f"close flush failed: {exc}")
                tasks = self._writer.detach_compression_tasks()
                error: SinkError | None = SinkError(
                    f"FileSink close failed for {self._path}: {exc}"
                )
                # 等待压缩在锁外进行，见下文。
            else:
                tasks = self._writer.detach_compression_tasks()
                error = None
        await self._await_compression(tasks)
        if error is not None:
            raise error

    async def write(self, batch: list[PointValue]) -> None:
        """序列化整批点值进缓冲区，达到阈值/间隔时刷盘，随后检查滚动。

        序列化是纯 CPU 内存操作，在拿锁之前完成；锁内只做缓冲、
        刷盘与滚动的文件状态转移。

        Args:
            batch: 待落盘的点值（由路由已分配到本 sink 的数据）。

        Raises:
            SinkError: 写文件失败（磁盘满、权限不足），或 ``open`` 尚未调用。
        """
        if not batch:
            return
        payload = b"".join(self._serialize_row(pv) for pv in batch)
        async with self._lock:
            if not self._writer.is_open:
                raise SinkError("FileSink.write() called before open()")
            try:
                await self._writer.append(payload, lines=len(batch))
            except OSError as exc:
                self._record_failure(f"write failed: {exc}")
                raise SinkError(f"FileSink write failed for {self._path}: {exc}") from exc
            self._mark_healthy()

    async def flush(self) -> None:
        """立即把缓冲区内容写入文件并冲刷到底层；未打开时无操作。

        Raises:
            SinkError: 刷盘失败（磁盘满、权限不足）时抛出。
        """
        async with self._lock:
            try:
                await self._writer.flush()
            except OSError as exc:
                self._record_failure(f"flush failed: {exc}")
                raise SinkError(f"FileSink flush failed for {self._path}: {exc}") from exc
            if self._writer.is_open:
                self._mark_healthy()

    def health(self) -> ConnectionHealth:
        """返回缓存的健康状态；连续写失败达到阈值后报告 unhealthy。"""
        return ConnectionHealth(healthy=self._healthy, message=self._error_message)

    # -- 内部：序列化（纯 CPU，无锁） --------------------------------------

    def _serialize_row(self, pv: PointValue) -> bytes:
        """把一个点值序列化为一行字节（含末尾换行符）。"""
        if self._format == "jsonl":
            line = json.dumps(
                {
                    "device_id": pv.device_id,
                    "point_id": pv.point_id,
                    "value": pv.value,
                    "quality": pv.quality.value,
                    "timestamp": pv.timestamp.isoformat(),
                    "source": pv.source,
                },
                ensure_ascii=False,
            )
            return (line + "\n").encode("utf-8")
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(
            [
                pv.device_id,
                pv.point_id,
                self._value_str(pv.value),
                pv.quality.value,
                pv.timestamp.isoformat(),
                pv.source or "",
            ]
        )
        return buf.getvalue().encode("utf-8")

    @staticmethod
    def _value_str(value: PointScalar) -> str:
        """CSV 单元格值转换：``None`` 写空串，其余用 ``str`` 表示。"""
        if value is None:
            return ""
        return str(value)

    # -- 内部：后台压缩等待（锁外） -----------------------------------------

    @staticmethod
    async def _await_compression(tasks: list[asyncio.Task[None]]) -> None:
        """等待后台压缩 task 完成；压缩 task 自身已吞掉异常并记录日志。"""
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- 内部：健康跟踪 ---------------------------------------------------

    def _mark_healthy(self) -> None:
        """成功写入/刷盘后复位失败计数、恢复健康。"""
        self._consecutive_failures = 0
        self._healthy = True
        self._error_message = None

    def _record_failure(self, message: str) -> None:
        """累计失败次数，达到阈值后标记 unhealthy（决策 8）。"""
        self._consecutive_failures += 1
        self._error_message = message
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            self._healthy = False
