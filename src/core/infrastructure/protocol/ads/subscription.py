"""使用 Driver 共享连接的 ADS 周期通知订阅。

原生 pyads 回调仅负责跨线程投递；一个有界队列和一个异步 Worker
负责顺序交付样本。连接恢复和订阅重建由上层统一管理。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from ctypes import c_ubyte
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import pyads  # type: ignore[import-untyped]

from core.application.errors import ConfigError
from core.application.protocol_contract import ProtocolSample, Quality, TimestampSource

from .codec import NotificationBlock, compile_notification_blocks
from .mapping import ADSPoint

logger = logging.getLogger(__name__)
_QUEUE_CAPACITY = 1000


class ADSSubscription:
    """一次订阅：多个 Notification Handle、一个队列、一个 Worker。"""

    def __init__(
        self,
        connection: Any,
        points: Sequence[ADSPoint],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        cycle_time: float,
        loop: asyncio.AbstractEventLoop,
        max_block_bytes: int = 1024,
    ) -> None:
        if cycle_time <= 0:
            raise ConfigError("ADS subscription interval must be > 0")
        self._connection = connection
        self._points = tuple(points)
        self._callback = callback
        self._cycle_time = cycle_time
        self._loop = loop
        self._max_block_bytes = max_block_bytes
        self._queue: asyncio.Queue[tuple[ProtocolSample, ...]] = asyncio.Queue(maxsize=_QUEUE_CAPACITY)
        self._worker_task: asyncio.Task[None] | None = None
        self._handles: list[tuple[Any, Any]] = []
        self._closed = False
        self._generation = 0
        self._dropped_count = 0

    async def start(self) -> None:
        """在共享连接上注册通知；失败时注销已注册项。"""
        if self._closed or self._worker_task is not None:
            raise RuntimeError("ADS subscription is closed or already started")
        generation = self._generation
        try:
            for block in compile_notification_blocks(
                self._points,
                max_bytes=self._max_block_bytes,
            ):
                await self._register_block(block, generation)
        except BaseException:
            await self._remove_handles()
            raise
        self._worker_task = asyncio.create_task(self._worker(), name="ads-subscription-worker")

    async def _register_block(self, block: NotificationBlock, generation: int) -> None:
        attr = pyads.NotificationAttrib(
            block.size,
            trans_mode=pyads.ADSTRANS_SERVERCYCLE,
            cycle_time=self._cycle_time,
        )
        buffer_type = c_ubyte * block.size
        wrapped = self._connection.notification(buffer_type)(
            self._block_callback(block, generation)
        )
        handle = await asyncio.to_thread(
            self._connection.add_device_notification,
            (block.group, block.offset),
            attr,
            wrapped,
        )
        self._handles.append(handle)

    def _block_callback(self, block: NotificationBlock, generation: int) -> Callable[..., None]:
        """一个 ADS 原生通知解码成一个批次，跨线程只投递一次。"""
        def on_notification(
            handle: Any, address: Any, timestamp: Any, value: Any
        ) -> None:
            del handle, address
            if self._closed or generation != self._generation:
                return
            received_at, source = _normalize_timestamp(timestamp)
            try:
                raw = bytes(value)
                if len(raw) != block.size:
                    raise ValueError("ADS notification block length mismatch")
            except (TypeError, ValueError):
                raw = b""
            samples: list[ProtocolSample] = []
            for point, offset, decoder in block.fields:
                try:
                    scalar = decoder(raw, offset) if len(raw) == block.size else None
                    quality = Quality.GOOD if len(raw) == block.size else Quality.BAD
                except (ValueError, TypeError, UnicodeDecodeError, struct.error):
                    scalar = None
                    quality = Quality.BAD
                samples.append(ProtocolSample(
                    point_id=point.point_id,
                    value=scalar,
                    quality=quality,
                    timestamp=received_at,
                    timestamp_source=source,
                ))
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._enqueue_batch, tuple(samples), generation)

        return on_notification

    async def close(self) -> None:
        """先阻止投递并注销通知，再取消 Worker；不关闭共享连接。"""
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        await self._remove_handles()
        task, self._worker_task = self._worker_task, None
        if task is not None:
            task.cancel()
            if task is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _remove_handles(self) -> None:
        handles, self._handles = self._handles, []
        for handle, user_handle in handles:
            try:
                await asyncio.to_thread(
                    self._connection.del_device_notification,
                    handle,
                    user_handle,
                )
            except Exception:
                logger.exception("ADS notification deregistration failed")

    def _enqueue_batch(self, samples: tuple[ProtocolSample, ...], generation: int) -> None:
        """只在事件循环线程入队；满时丢弃最旧的整个 Block。"""
        if self._closed or generation != self._generation:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self._dropped_count += 1
            if self._dropped_count == 1 or self._dropped_count % 1000 == 0:
                logger.warning(
                    "ADS subscription queue dropped %d blocks", self._dropped_count
                )
        self._queue.put_nowait(samples)

    async def _worker(self) -> None:
        while True:
            samples = await self._queue.get()
            for sample in samples:
                try:
                    await self._callback(sample)
                except Exception:
                    logger.exception(
                        "ADS subscription callback failed for '%s'", sample.point_id
                    )


def _normalize_timestamp(value: object) -> tuple[datetime, TimestampSource]:
    """优先采用 pyads 通知时标，无效时才使用本地 UTC 接收时间。"""
    if not isinstance(value, datetime):
        return datetime.now(UTC), "local"
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC), "device"
    return value.astimezone(UTC), "device"
