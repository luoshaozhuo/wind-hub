"""单次 ADS Notification 订阅；start 注册，stop 完整清理。"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable, Sequence
from ctypes import c_ubyte
from datetime import UTC, datetime
from typing import Any

import pyads  # type: ignore[import-untyped]

from core.application.port.protocol import ProtocolSampleCallback
from core.application.protocol_contract import ProtocolSample, Quality

from .codec import NotificationBlock, compile_notification_blocks
from .mapping import ADSPoint
from .session import _AdsSession


class ADSSubscription:
    """独占一组 Notification Handle；复用 Driver 的唯一 Session。"""

    def __init__(
        self,
        session: _AdsSession,
        points: Sequence[ADSPoint],
        callback: ProtocolSampleCallback,
        *,
        interval: float,
        max_block_bytes: int,
        max_delay: float,
    ) -> None:
        if interval <= 0:
            raise ValueError("ADS notification interval must be positive")
        self._session = session
        self._blocks = compile_notification_blocks(points, max_bytes=max_block_bytes)
        self._callback = callback
        self._interval = interval
        self._max_delay = max_delay
        self._loop = asyncio.get_running_loop()
        self._handles: list[tuple[Any, Any]] = []
        self._callbacks: list[Callable[..., None]] = []
        self._queue: asyncio.Queue[tuple[ProtocolSample, ...]] = asyncio.Queue(maxsize=1000)
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._stopping or self._start_task is not None:
            raise RuntimeError("ADS subscription can only start once")
        self._start_task = asyncio.create_task(self._register_all())
        try:
            await asyncio.shield(self._start_task)
        except BaseException:
            # start 取消后注册可能迟到成功；独立的 stop 任务负责注销。
            self._begin_stop()
            raise

    async def _register_all(self) -> None:
        try:
            for block in self._blocks:
                if self._stopping:
                    break
                attribute = pyads.NotificationAttrib(
                    block.size,
                    trans_mode=pyads.ADSTRANS_SERVERCYCLE,
                    cycle_time=self._interval * 1000,
                    max_delay=self._max_delay * 1000,
                )
                callback = self._notification_callback(block)
                self._callbacks.append(callback)
                token = await self._session.execute(
                    "add_device_notification",
                    (block.group, block.offset),
                    attribute,
                    callback,
                    None,
                )
                self._handles.append(token)
            if not self._stopping:
                self._worker = asyncio.create_task(self._consume())
        except BaseException:
            self._begin_stop()
            raise

    def _notification_callback(self, block: NotificationBlock) -> Callable[..., None]:
        def receive(_handle: Any, _address: Any, timestamp: Any, value: Any) -> None:
            if self._stopping:
                return
            received_at = datetime.now(UTC)
            source = "device" if isinstance(timestamp, datetime) else "local"
            stamp = timestamp if isinstance(timestamp, datetime) else received_at
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            stamp = stamp.astimezone(UTC)
            try:
                raw = bytes(value)
                valid = len(raw) == block.size
            except (TypeError, ValueError):
                raw = b""
                valid = False
            samples: list[ProtocolSample] = []
            for point, offset, decoder in block.fields:
                try:
                    scalar = decoder(raw, offset) if valid else None
                    quality = Quality.GOOD if valid else Quality.BAD
                except (TypeError, ValueError, struct.error, UnicodeDecodeError):
                    scalar, quality = None, Quality.BAD
                samples.append(ProtocolSample(
                    point_id=point.point_id,
                    value=scalar,
                    quality=quality,
                    timestamp=stamp,
                    timestamp_source=source,
                    receive_time=received_at,
                ))
            try:
                self._loop.call_soon_threadsafe(self._enqueue, tuple(samples))
            except RuntimeError:
                # 事件循环结束时的迟到原生回调：不可跨线程再次提交。
                return
        return receive

    def _enqueue(self, samples: tuple[ProtocolSample, ...]) -> None:
        if self._stopping:
            return
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(samples)

    async def _consume(self) -> None:
        while True:
            batch = await self._queue.get()
            try:
                await self._callback(batch)
            except Exception:
                # 业务回调失败不终止原生订阅，由调用方记录后续数据。
                pass

    def _begin_stop(self) -> asyncio.Task[None]:
        if self._stop_task is None:
            self._stopping = True
            self._stop_task = asyncio.create_task(self._stop_once())
        return self._stop_task

    async def stop(self) -> None:
        await asyncio.shield(self._begin_stop())

    async def _stop_once(self) -> None:
        if self._start_task is not None:
            try:
                await asyncio.shield(self._start_task)
            except Exception:
                pass
        errors: list[Exception] = []
        for token in reversed(self._handles):
            try:
                await self._session.execute("del_device_notification", *token)
            except Exception as exc:
                errors.append(exc)
        self._handles.clear()
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if errors:
            raise ExceptionGroup("ADS notification cleanup failed", errors)
