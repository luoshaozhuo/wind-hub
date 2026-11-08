"""单次 ADS Notification 订阅；start 注册，stop 完整清理。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import struct
from collections.abc import Callable, Sequence
from ctypes import c_ubyte
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import pyads  # type: ignore[import-untyped]

from core.application.port.protocol import ProtocolSampleCallback
from core.application.protocol_contract import ProtocolSample, Quality
from core.infrastructure.protocol.mapping_barrier import MappingBarrier

from .codec import NotificationBlock, compile_notification_blocks
from .mapping import ADSPoint
from .session import ADSCommandNotSentError, _AdsSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PreparedSubscription:
    points: tuple[ADSPoint, ...]
    blocks: tuple[NotificationBlock, ...]
    generation: int
    handles: tuple[tuple[Any, Any], ...]
    callbacks: tuple[Callable[..., None], ...]


class ADSSubscription:
    """独占一组 Notification Handle；复用 Driver 的唯一 Session。"""

    def __init__(
        self,
        session: _AdsSession,
        points: Sequence[ADSPoint],
        callback: ProtocolSampleCallback,
        *,
        interval: float,
        trigger: Literal["cyclic", "on_change"],
        max_block_bytes: int,
        max_delay: float,
        queue_capacity: int,
        barrier: MappingBarrier,
        on_stopped: Callable[[ADSSubscription], None] | None = None,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("ADS notification interval must be positive and finite")
        if queue_capacity <= 0:
            raise ValueError("ADS notification queue capacity must be positive")
        if trigger not in {"cyclic", "on_change"}:
            raise ValueError("unsupported ADS notification trigger")
        self._session = session
        self._barrier = barrier
        self.point_ids = frozenset(point.point_id for point in points)
        self._generation = 0
        self.delivery_lock = asyncio.Lock()
        self._retired_handles: list[tuple[Any, Any]] = []
        self._prepared_handles: list[tuple[Any, Any]] = []
        self._on_stopped = on_stopped
        self._max_block_bytes = max_block_bytes
        self._blocks = compile_notification_blocks(points, max_bytes=max_block_bytes)
        self._callback = callback
        self._interval = interval
        self._trigger = trigger
        self._max_delay = max_delay
        self._loop = asyncio.get_running_loop()
        self._handles: list[tuple[Any, Any]] = []
        self._callbacks: list[Callable[..., None]] = []
        self._queue: asyncio.Queue[tuple[int, tuple[ProtocolSample, ...]]] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._start_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._registration_uncertain = False
        self._cleanup_retryable = False
        self.dropped_notifications = 0
        self.callback_failures = 0

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

    def _notification_attrib(self, size: int) -> pyads.NotificationAttrib:
        """Use the same PLC-native trigger during registration and hot updates."""
        trans_mode = (
            pyads.ADSTRANS_SERVERCYCLE
            if self._trigger == "cyclic"
            else pyads.ADSTRANS_SERVERONCHA
        )
        return pyads.NotificationAttrib(
            size,
            trans_mode=trans_mode,
            cycle_time=self._interval * 1000,
            max_delay=self._max_delay * 1000,
        )

    async def _register_all(self) -> None:
        try:
            for block in self._blocks:
                if self._stopping:
                    break
                attribute = self._notification_attrib(block.size)
                callback = self._session.notification(c_ubyte * block.size)(
                    self._notification_callback(block, self._generation)
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

    def _notification_callback(
        self, block: NotificationBlock, generation: int
    ) -> Callable[..., None]:
        def receive(_handle: Any, _address: Any, timestamp: Any, value: Any) -> None:
            if self._stopping:
                return
            received_at = datetime.now(UTC)
            # A timezone-naive PLC timestamp has no reliable UTC meaning.
            # Do not silently label it as an authoritative device clock.
            valid_device_time = (
                isinstance(timestamp, datetime)
                and timestamp.tzinfo is not None
                and timestamp.utcoffset() is not None
            )
            source: Literal["device", "local"] = (
                "device" if valid_device_time else "local"
            )
            stamp = timestamp.astimezone(UTC) if valid_device_time else received_at
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
                self._loop.call_soon_threadsafe(self._enqueue, generation, tuple(samples))
            except RuntimeError:
                # 事件循环结束时的迟到原生回调：不可跨线程再次提交。
                return

        return receive

    def _enqueue(self, generation: int, samples: tuple[ProtocolSample, ...]) -> None:
        if self._stopping or generation != self._generation:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped_notifications += 1
            count = self.dropped_notifications
            if count & (count - 1) == 0:
                logger.warning(
                    "ADS notification queue overflow: %s oldest batches discarded",
                    count,
                )
        self._queue.put_nowait((generation, samples))

    async def _consume(self) -> None:
        while True:
            generation, batch = await self._queue.get()
            try:
                async with self.delivery_lock:
                    if not self._stopping and generation == self._generation:
                        with self._barrier.callback_scope():
                            await self._callback(batch)
            except Exception:
                self.callback_failures += 1
                logger.exception("ADS notification delivery callback failed")

    async def prepare_update(self, points: Sequence[ADSPoint]) -> _PreparedSubscription:
        """暂存新原生句柄；当前交付代次保持不变。"""
        if self._stopping:
            raise RuntimeError("ADS subscription is stopping")
        blocks = tuple(compile_notification_blocks(points, max_bytes=self._max_block_bytes))
        generation = self._generation + 1
        # start 尚未被调用时无需远端预注册。
        if self._start_task is None:
            return _PreparedSubscription(tuple(points), blocks, generation, (), ())
        await asyncio.shield(self._start_task)
        handles: list[tuple[Any, Any]] = []
        callbacks: list[Callable[..., None]] = []
        try:
            for block in blocks:
                attribute = self._notification_attrib(block.size)
                callback = self._session.notification(c_ubyte * block.size)(
                    self._notification_callback(block, generation)
                )
                callbacks.append(callback)
                token = await self._session.execute(
                    operation="add_device_notification",
                    data=(block.group, block.offset),
                    attr=attribute,
                    callback=callback,
                    user_handle=None,
                )
                handles.append(token)
        except BaseException as exc:
            # 非“确认未发送”的失败不能证明没有注册成功的 Native Handle。
            if not isinstance(exc, ADSCommandNotSentError):
                self._registration_uncertain = True
            self._retired_handles.extend(handles)
            self._callbacks.extend(callbacks)
            raise
        # 从注册成功到 Driver 提交之间保持 Native Handle 所有权；
        # 即使外层取消，stop() 仍能找到候选句柄。
        self._prepared_handles.extend(handles)
        self._callbacks.extend(callbacks)
        return _PreparedSubscription(
            tuple(points), blocks, generation, tuple(handles), tuple(callbacks)
        )

    def commit_update(self, candidate: _PreparedSubscription) -> None:
        """必须由 Driver 在持有 delivery_lock 时与映射引用同步提交。"""
        for token in candidate.handles:
            self._prepared_handles.remove(token)
        self._retired_handles.extend(self._handles)
        self._handles = list(candidate.handles)
        # Candidate callbacks were retained at prepare time and must not
        # accumulate duplicate strong references on each live table update.
        self._blocks = tuple(candidate.blocks)
        self.point_ids = frozenset(point.point_id for point in candidate.points)
        self._generation = candidate.generation
        while not self._queue.empty():
            self._queue.get_nowait()

    async def cleanup_retired(self) -> tuple[str, ...]:
        """清理旧代次 Native Handles，失败者保留给 stop/close 再处理。"""
        pending = list(self._retired_handles)
        remaining: list[tuple[Any, Any]] = []
        for token in pending:
            try:
                await self._session.execute(
                    operation="del_device_notification",
                    notification_handle=token[0],
                    user_handle=token[1],
                )
            except Exception:
                remaining.append(token)
        self._retired_handles = remaining
        # 回调 C 指针在 Session 最终关闭前保持强引用，避免迟到原生回调悬空。
        return tuple(f"ADS notification:{index}" for index in range(len(remaining)))

    async def abandon_prepared(self, candidate: _PreparedSubscription) -> tuple[str, ...]:
        """预提交失败时只撤销候选资源，不触碰旧可用句柄。"""
        for token in candidate.handles:
            self._prepared_handles.remove(token)
        self._retired_handles.extend(candidate.handles)
        return await self.cleanup_retired()

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
        if asyncio.current_task() is self._worker:
            # 通知回调内停止自身：不等待当前工作 Task 结束，交给独立清理 Task。
            self._begin_stop()
            return
        await asyncio.shield(self._begin_stop())

    async def _stop_once(self) -> None:
        if self._start_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._start_task)
        errors: list[Exception] = []
        self._cleanup_retryable = True
        remaining: list[tuple[Any, Any]] = []
        for token in reversed([*self._handles, *self._retired_handles, *self._prepared_handles]):
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
        self._retired_handles.clear()
        self._prepared_handles.clear()
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
