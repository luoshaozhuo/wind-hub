"""本地文件输出 sink —— 追加写 CSV / JSONL，支持滚动策略与后台压缩。

实现 :class:`~wind_hub.application.port.sink.SinkPort` 的真实文件走向：把一批
:class:`~wind_hub.domain.model.point.PointValue` 序列化成 ``csv`` 或 ``jsonl``
行，追加写入本地文件；滚动判断委托给 :class:`~.rotation.RotationPolicy`（由
``params`` 里的 ``max_size_mb`` / ``max_age_hours`` 构建），滚动把当前文件改名
成 ``{base}.{suffix}.{ext}`` 后另开新文件，并对旧分片异步压缩（``compress`` 参数
开启时）；``buffer_size`` / ``flush_interval`` 控制刷盘时机，``close`` 时强制
flush。

所有文件级参数从 ``SinkConfig.params`` 读取并在**构造时**校验；写文件失败
（磁盘满、权限不足等）抛 :class:`~wind_hub.domain.model.errors.SinkError`，
连续失败达到阈值后 ``health()`` 报告 unhealthy。压缩失败仅记录日志，不影响
采集主流程。
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from wind_hub.adapter.outbound.sink.file.compression import (
    Compressor,
    GzipCompressor,
    NoCompressor,
)
from wind_hub.adapter.outbound.sink.file.rotation import build_rotation
from wind_hub.application.port.sink import SinkPort
from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.errors import ConfigError, SinkError
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus
from wind_hub.infra import metrics

logger = logging.getLogger(__name__)

# CSV 表头固定列序，与 `_serialize` 的 CSV 行一致。
_CSV_HEADER = ["device_id", "point_id", "value", "quality", "timestamp", "source"]

# 连续写/刷盘失败达到该次数后，sink 标记为 unhealthy（决策 8）。
_MAX_CONSECUTIVE_FAILURES = 5


class FileSink(SinkPort):
    """追加写本地文件的输出 sink。

    参数（``SinkConfig.params``）：

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
        params = config.params

        path = params.get("path")
        if not isinstance(path, str) or not path:
            raise ConfigError("FileSink 'path' is required and must be a non-empty string")
        self._path = Path(path)

        fmt = params.get("format", "jsonl")
        if fmt not in ("csv", "jsonl"):
            raise ConfigError(f"FileSink 'format' must be 'csv' or 'jsonl', got {fmt!r}")
        self._format = fmt

        self._rotation = build_rotation(params)
        self._buffer_size = self._positive_int(params.get("buffer_size", 100), "buffer_size")
        self._flush_interval = self._nonnegative_number(
            params.get("flush_interval", 1.0), "flush_interval"
        )
        self._write_header = bool(params.get("write_header", True))
        self._compressor: Compressor = self._build_compressor(params)

        # 运行时状态 —— 由 `asyncio.Lock` 保护，仅在同一事件循环内被调度器调用。
        self._name = config.name
        self._file: BinaryIO | None = None
        self._buffer: list[str] = []
        self._current_size = 0
        self._opened_at = 0.0
        self._last_flush = 0.0
        self._lock = asyncio.Lock()
        self._healthy = True
        self._error_message: str | None = None
        self._consecutive_failures = 0
        # 滚动触发的后台压缩 task；`close` 时等待其完成，避免解释器退出告警。
        self._compress_tasks: set[asyncio.Task[None]] = set()

    # -- 参数校验 ---------------------------------------------------------

    @staticmethod
    def _build_compressor(params: dict[str, Any]) -> Compressor:
        """从 ``params`` 构建压缩器；``compress`` 关闭时返回空实现。"""
        if not bool(params.get("compress", False)):
            return NoCompressor()
        level = params.get("compress_level", 6)
        if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 9:
            raise ConfigError(
                f"FileSink 'compress_level' must be an integer in [1, 9], got {level!r}"
            )
        return GzipCompressor(level=level)

    @staticmethod
    def _positive_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"FileSink '{field}' must be a positive integer, got {value!r}")
        return value

    @staticmethod
    def _nonnegative_number(value: object, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise ConfigError(f"FileSink '{field}' must be a non-negative number, got {value!r}")
        return float(value)

    # -- SinkPort 契约 ----------------------------------------------------

    async def open(self) -> None:
        """创建父目录并打开文件（追加模式）；幂等，已打开时无操作。

        Raises:
            SinkError: 路径不可写（父目录无法创建、权限不足等）时抛出。
        """
        async with self._lock:
            if self._file is not None:
                return
            try:
                self._open_file()
            except OSError as exc:
                self._record_failure(f"open failed: {exc}")
                raise SinkError(f"FileSink open failed for {self._path}: {exc}") from exc

    async def close(self) -> None:
        """强制 flush 缓冲后关闭文件并等待后台压缩完成；幂等，已关闭时无操作。

        Raises:
            SinkError: 冲刷或关闭文件失败时抛出（文件句柄仍被释放）。
        """
        async with self._lock:
            if self._file is None:
                await self._await_compression()
                return
            try:
                self._flush_locked()
            except OSError as exc:
                self._file.close()
                self._file = None
                self._record_failure(f"close flush failed: {exc}")
                await self._await_compression()
                raise SinkError(f"FileSink close failed for {self._path}: {exc}") from exc
            self._file.close()
            self._file = None
            await self._await_compression()

    async def write(self, batch: list[PointValue]) -> None:
        """序列化整批点值进缓冲区，达到阈值/间隔时刷盘，随后检查滚动。

        Args:
            batch: 待落盘的点值（由路由已分配到本 sink 的数据）。

        Raises:
            SinkError: 写文件失败（磁盘满、权限不足），或 ``open`` 尚未调用。
        """
        if not batch:
            return
        async with self._lock:
            if self._file is None:
                raise SinkError("FileSink.write() called before open()")
            try:
                for pv in batch:
                    self._buffer.append(self._serialize(pv))
                if self._should_flush():
                    self._flush_locked()
                if self._check_rollover():
                    self._rollover()
            except OSError as exc:
                metrics.sink_write_failures_total.labels(sink_name=self._name).inc()
                self._record_failure(f"write failed: {exc}")
                raise SinkError(f"FileSink write failed for {self._path}: {exc}") from exc
            metrics.sink_writes_total.labels(sink_name=self._name).inc()
            metrics.sink_points_written_total.labels(sink_name=self._name).inc(len(batch))
            self._mark_healthy()

    async def flush(self) -> None:
        """立即把缓冲区内容写入文件并冲刷到底层；未打开时无操作。

        Raises:
            SinkError: 刷盘失败（磁盘满、权限不足）时抛出。
        """
        async with self._lock:
            if self._file is None:
                return
            try:
                self._flush_locked()
            except OSError as exc:
                self._record_failure(f"flush failed: {exc}")
                raise SinkError(f"FileSink flush failed for {self._path}: {exc}") from exc
            self._mark_healthy()

    def health(self) -> HealthStatus:
        """返回缓存的健康状态；连续写失败达到阈值后报告 unhealthy。"""
        return HealthStatus(healthy=self._healthy, message=self._error_message)

    # -- 内部：序列化 / 刷盘 / 滚动 ---------------------------------------

    def _serialize(self, pv: PointValue) -> str:
        """把一个点值序列化为一行（不带末尾换行符）。"""
        if self._format == "jsonl":
            return json.dumps(
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
        return buf.getvalue().rstrip("\n")

    @staticmethod
    def _value_str(value: Any) -> str:
        """CSV 单元格值转换：``None`` 写空串，其余用 ``str`` 表示。"""
        if value is None:
            return ""
        return str(value)

    def _should_flush(self) -> bool:
        """缓冲行数达到阈值，或距上次刷盘超过 ``flush_interval`` 秒。"""
        if len(self._buffer) >= self._buffer_size:
            return True
        if self._flush_interval > 0:
            return time.monotonic() - self._last_flush >= self._flush_interval
        return False

    def _flush_locked(self) -> None:
        """把缓冲区写入文件并冲刷；调用方须已持有锁且文件已打开。"""
        if not self._buffer:
            return
        assert self._file is not None
        payload = ("\n".join(self._buffer) + "\n").encode("utf-8")
        self._file.write(payload)
        self._file.flush()
        self._current_size += len(payload)
        self._buffer.clear()
        self._last_flush = time.monotonic()

    def _check_rollover(self) -> bool:
        """当前文件是否达到滚动条件（委托给滚动策略）。"""
        return self._rotation.should_rotate(self._current_size, time.monotonic() - self._opened_at)

    def _rollover(self) -> None:
        """关闭当前文件、改名归档、异步压缩旧分片、重开新文件。

        调用方须已持有锁且文件已打开。
        """
        assert self._file is not None
        self._file.close()
        self._file = None
        rolled = self._rolled_path()
        self._path.rename(rolled)
        self._schedule_compression(rolled)
        self._open_file()

    def _rolled_path(self) -> Path:
        """滚动文件名 ``{base}.{suffix}.{ext}``，后缀由滚动策略生成。"""
        ts = self._rotation.rotation_suffix(datetime.now(UTC))
        return self._path.with_name(f"{self._path.stem}.{ts}{self._path.suffix}")

    def _open_file(self) -> None:
        """创建父目录、以追加模式打开文件；空 CSV 文件补写表头。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._current_size = self._path.stat().st_size if self._path.exists() else 0
        # 文件句柄在此后整个 sink 生命周期内保持打开（追加 + 显式 flush/close），
        # 不在 with 块内，故此处豁免 SIM115。
        self._file = open(self._path, "ab")  # noqa: SIM115
        if self._format == "csv" and self._write_header and self._current_size == 0:
            header = (",".join(_CSV_HEADER) + "\n").encode("utf-8")
            self._file.write(header)
            self._current_size += len(header)
        self._opened_at = time.monotonic()
        self._last_flush = time.monotonic()

    # -- 内部：后台压缩 ---------------------------------------------------

    def _schedule_compression(self, path: Path) -> None:
        """把旧分片交给后台 task 压缩，不阻塞写入路径。"""
        if isinstance(self._compressor, NoCompressor):
            return
        task = asyncio.create_task(self._compress_async(path))
        self._compress_tasks.add(task)
        task.add_done_callback(self._compress_tasks.discard)

    async def _compress_async(self, path: Path) -> None:
        """在独立 task 里压缩；文件 I/O 放到线程池，失败仅记录日志。"""
        try:
            await asyncio.to_thread(self._compressor.compress, path)
            logger.info("compressed archived file %s", path)
        except Exception as exc:
            # 压缩是采集主流程之外的 best-effort 增强：失败只记录日志，
            # 不得让后台 task 的异常中断滚动/写入。
            logger.warning("compression failed for %s: %s", path, exc)

    async def _await_compression(self) -> None:
        """等待所有后台压缩 task 完成（``close`` 时调用）。"""
        if self._compress_tasks:
            await asyncio.gather(*list(self._compress_tasks), return_exceptions=True)
            self._compress_tasks.clear()

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
