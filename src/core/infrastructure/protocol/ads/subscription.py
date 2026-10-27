"""单次 ADS Notification 订阅；start 注册，stop 完整清理。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Callable, Sequence
from ctypes import c_ubyte
from datetime import UTC, datetime
from typing import Any, Literal

import pyads  # type: ignore[import-untyped]

from core.application.port.protocol import ProtocolSampleCallback
from core.application.protocol_contract import ProtocolSample, Quality

from .codec import NotificationBlock, compile_notification_blocks
from .mapping import ADSPoint
from .session import ADSCommandNotSentError, _AdsSession

logger = logging.getLogger(__name__)


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
        on_stopped: Callable[[ADSSubscription], None] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("ADS notification interval must be positive")
        self._session = session
        self._on_stopped = on_stopped
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
        self._registration_uncertain = False
        self._cleanup_retryable = False

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
                callback = self._session.notification(c_ubyte * block.size)(
                    self._notification_callback(block)
                )
                self._callbacks.append(callback)
                try:
                    # data: 通知块的 Index Group/Offset；attr: 长度与采样周期；
                    # callback: pyads 原生通知回调；user_handle: 不额外申请 Symbol 句柄。
                    token = await self._session.execute(
                        operation="add_device_notification",
                        data=(block.group, block.offset),
                        attr=attribute,
                        callback=callback,
                        user_handle=None,
                    )
                except ADSCommandNotSentError:
                    raise
                except BaseException:
                    self._registration_uncertain = True
                    raise
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
            source: Literal["device", "local"] = (
                "device" if isinstance(timestamp, datetime) else "local"
            )
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
                samples.append(
                    ProtocolSample(
                        point_id=point.point_id,
                        value=scalar,
                        quality=quality,
                        timestamp=stamp,
                        timestamp_source=source,
                        receive_time=received_at,
                    )
                )
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
                logger.exception("ADS notification delivery callback failed")

    def _begin_stop(self) -> asyncio.Task[None]:
        if self._stop_task is None or (
            self._stop_task.done()
            and self._stop_task.exception() is not None
            and self._cleanup_retryable
        ):
            self._stopping = True
            self._stop_task = asyncio.create_task(self._stop_once())
        return self._stop_task

    async def stop(self) -> None:
        await asyncio.shield(self._begin_stop())

    async def _stop_once(self) -> None:
        if self._start_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._start_task)
        errors: list[Exception] = []
        self._cleanup_retryable = True
        remaining: list[tuple[Any, Any]] = []
        for token in reversed(self._handles):
            try:
                # notification_handle/user_handle: 注册时返回的通知与用户句柄。
                await self._session.execute(
                    operation="del_device_notification",
                    notification_handle=token[0],
                    user_handle=token[1],
                )
            except Exception as exc:
                if not isinstance(exc, ADSCommandNotSentError):
                    self._cleanup_retryable = False
                errors.append(exc)
                remaining.append(token)
        self._handles = list(reversed(remaining))
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        if self._registration_uncertain:
            self._cleanup_retryable = False
            errors.append(RuntimeError("ADS notification registration result unconfirmed"))
        if errors:
            raise ExceptionGroup("ADS notification cleanup failed", errors)
        if self._on_stopped is not None:
            self._on_stopped(self)
