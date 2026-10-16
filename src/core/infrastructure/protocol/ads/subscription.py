"""使用 Driver 共享连接的 ADS 周期通知订阅。

原生 pyads 回调仅负责跨线程投递；一个有界队列和一个异步 Worker
负责顺序交付样本。连接恢复和订阅重建由上层统一管理。

生命周期分为三个相互独立、各自幂等的阶段，可单独调用，也可由
:meth:`ADSSubscription.close` 一次性完成：

1. ``deactivate``：同步停止新通知投递（generation 失效）；
2. ``deregister``：等待在途注册返回并注销全部确认持有的 Handle；
3. ``stop_worker``：取消 Worker 并清空队列。

每个阶段只创建一个后台任务；协程被取消时清理任务仍会执行完毕，
后续调用（或重复 close）通过同一个任务收敛，保证注销/取消不重不漏。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Awaitable, Callable, Sequence
from ctypes import c_ubyte
from datetime import UTC, datetime
from typing import Any, cast

import pyads  # type: ignore[import-untyped]

from core.application.errors import ConfigError
from core.application.protocol_contract import ProtocolSample, Quality, TimestampSource

from .codec import NotificationBlock, compile_notification_blocks
from .mapping import ADSPoint

logger = logging.getLogger(__name__)
_QUEUE_CAPACITY = 1000


class ADSSubscription:
    """一次订阅：多个 Notification Handle、一个队列、一个 Worker。

    ``session`` 必须提供同步方法 ``run(name, *args)``（在工作线程中
    执行 pyads 命令）与 ``notification(datatype)`` 装饰器。
    """

    def __init__(
        self,
        session: Any,
        points: Sequence[ADSPoint],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        cycle_time: float,
        loop: asyncio.AbstractEventLoop,
        max_block_bytes: int = 1024,
    ) -> None:
        if cycle_time <= 0:
            raise ConfigError("ADS subscription interval must be > 0")
        self._session = session
        self._points = tuple(points)
        self._callback = callback
        self._cycle_time = cycle_time
        self._loop = loop
        self._max_block_bytes = max_block_bytes
        self._queue: asyncio.Queue[tuple[ProtocolSample, ...]] = asyncio.Queue(
            maxsize=_QUEUE_CAPACITY
        )
        self._worker_task: asyncio.Task[None] | None = None
        # 已确认注册成功、客户端持有凭据的 Handle。
        self._handles: list[tuple[Any, Any]] = []
        # DEL 失败：服务端是否已注销无法确认，只能记录为不确定残留，
        # 不能把本地移除当作注销成功。
        self._unconfirmed_handles: list[tuple[Any, Any]] = []
        # 已发起但结果尚未返回的注册任务（外层协程可能已被取消）。
        self._pending_registrations: set[asyncio.Task[tuple[Any, Any]]] = set()
        self._deactivated = False
        self._generation = 0
        self._dropped_count = 0
        self._deregister_task: asyncio.Task[None] | None = None
        self._stop_worker_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def unconfirmed_handles(self) -> tuple[tuple[Any, Any], ...]:
        """DEL 未确认的 Handle 凭据；空元组表示无不确定残留。"""
        return tuple(self._unconfirmed_handles)

    async def start(self) -> None:
        """在共享连接上注册通知；失败（含取消）时清理全部已注册项。"""
        if self._worker_task is not None or self._close_task is not None:
            raise RuntimeError("ADS subscription is closed or already started")
        generation = self._generation
        try:
            for block in compile_notification_blocks(
                self._points,
                max_bytes=self._max_block_bytes,
            ):
                await self._register_block(block, generation)
        except BaseException:
            # 普通失败：等待清理完成后再抛；取消：清理任务在后台继续，
            # 由后续 close/deregister 收敛（结果不确定的 Handle 会被记录）。
            if not self._start_cancelled():
                await self.close()
            else:
                self._begin_close()
            raise
        self._worker_task = asyncio.create_task(self._worker(), name="ads-subscription-worker")

    @staticmethod
    def _start_cancelled() -> bool:
        try:
            task = asyncio.current_task()
        except RuntimeError:  # pragma: no cover - 仅解释器关闭期 GC 可达
            return False
        return task is not None and task.cancelling() > 0

    async def _register_block(self, block: NotificationBlock, generation: int) -> None:
        # pyads NotificationAttrib 的 cycle_time 单位是毫秒（内部换算为
        # 100ns 单位）；self._cycle_time 为秒。
        attr = pyads.NotificationAttrib(
            block.size,
            trans_mode=pyads.ADSTRANS_SERVERCYCLE,
            cycle_time=self._cycle_time * 1000.0,
        )
        buffer_type = c_ubyte * block.size
        wrapped = self._session.notification(buffer_type)(self._block_callback(block, generation))
        # 注册任务独立于等待者：外层协程被取消后同步线程仍可能成功返回
        # Handle；凭据在任务体内、to_thread 返回的那一刻入表，与等待者
        # 是否取消无关，避免“服务端已注册、客户端丢失 Handle”。
        register_task = asyncio.create_task(
            self._add_notification((block.group, block.offset), attr, wrapped),
            name="ads-add-device-notification",
        )
        self._pending_registrations.add(register_task)
        register_task.add_done_callback(self._pending_registrations.discard)
        await asyncio.shield(register_task)

    async def _add_notification(
        self,
        address: tuple[int, int],
        attr: pyads.NotificationAttrib,
        wrapped: Callable[..., None],
    ) -> tuple[Any, Any]:
        handle = await asyncio.to_thread(
            self._session.run,
            "add_device_notification",
            address,
            attr,
            wrapped,
        )
        # 此刻同步注册已确认成功：先记录凭据，任务再返回。
        confirmed = cast(tuple[Any, Any], handle)
        self._handles.append(confirmed)
        return confirmed

    def _block_callback(self, block: NotificationBlock, generation: int) -> Callable[..., None]:
        """一个 ADS 原生通知解码成一个批次，跨线程只投递一次。"""

        def on_notification(handle: Any, address: Any, timestamp: Any, value: Any) -> None:
            del handle, address
            if self._deactivated or generation != self._generation:
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
                samples.append(
                    ProtocolSample(
                        point_id=point.point_id,
                        value=scalar,
                        quality=quality,
                        timestamp=received_at,
                        timestamp_source=source,
                    )
                )
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._enqueue_batch, tuple(samples), generation)

        return on_notification

    def deactivate(self) -> None:
        """同步阻止任何后续通知投递；幂等；不注销 Handle、不取消 Worker。"""
        if self._deactivated:
            return
        self._deactivated = True
        self._generation += 1

    async def deregister(self) -> None:
        """等待在途注册并注销全部 Notification Handle。

        幂等且并发安全：只创建一个注销任务，重复/并发调用收敛到同一任务。
        单个 Handle 注销失败不阻断其余 Handle，失败 Handle 记录到
        ``unconfirmed_handles``。
        """
        if self._deregister_task is None:
            self._deregister_task = asyncio.create_task(
                self._deregister_all(), name="ads-deregister-subscription"
            )
        await asyncio.shield(self._deregister_task)

    async def _deregister_all(self) -> None:
        # 先接住所有在途注册的结果（含外层取消后晚到的成功注册），
        # 避免把刚注册成功的 Handle 漏掉。
        if self._pending_registrations:
            await asyncio.gather(*self._pending_registrations, return_exceptions=True)
        while self._handles:
            handle = self._handles.pop(0)
            try:
                await asyncio.to_thread(
                    self._session.run,
                    "del_device_notification",
                    handle[0],
                    handle[1],
                )
            except Exception:
                # 服务端可能已注销也可能未注销：本地状态不确定，显式记录。
                self._unconfirmed_handles.append(handle)
                logger.exception(
                    "ADS notification deregistration failed for handle %r",
                    handle[0],
                )
        if self._unconfirmed_handles:
            logger.warning(
                "ADS subscription closed with %d unconfirmed notification handle(s)",
                len(self._unconfirmed_handles),
            )

    async def stop_worker(self) -> None:
        """取消 Worker 并清空残留批次；不触碰共享连接；幂等。"""
        worker = self._worker_task
        current = asyncio.current_task()
        if worker is not None and current is not None and worker is current:
            # Worker 在用户回调内关闭自身：不能自我 await。先投递取消，
            # 待当前回调结束回到队列等待点时生效。
            worker.cancel()
            self._worker_task = None
            self._drain_queue()
            return
        if self._stop_worker_task is None:
            self._stop_worker_task = asyncio.create_task(
                self._stop_worker_all(), name="ads-stop-subscription-worker"
            )
        await asyncio.shield(self._stop_worker_task)

    async def _stop_worker_all(self) -> None:
        task, self._worker_task = self._worker_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._drain_queue()

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    def _begin_close(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_all(), name="ads-close-subscription")
        return self._close_task

    async def close(self) -> None:
        """停投递 → 注销通知 → 取消 Worker；不关闭共享连接。

        幂等且并发安全：只创建一个关闭任务，多个并发 close 收敛到同一任务。
        调用方协程被取消不会中断实际清理；再次 await close 可等待清理完成。
        """
        task = self._begin_close()
        await asyncio.shield(task)

    async def _close_all(self) -> None:
        self.deactivate()
        try:
            await self.deregister()
        finally:
            await self.stop_worker()

    def _enqueue_batch(self, samples: tuple[ProtocolSample, ...], generation: int) -> None:
        """只在事件循环线程入队；满时丢弃最旧的整个 Block。"""
        if self._deactivated or generation != self._generation:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self._dropped_count += 1
            if self._dropped_count == 1 or self._dropped_count % 1000 == 0:
                logger.warning("ADS subscription queue dropped %d blocks", self._dropped_count)
        self._queue.put_nowait(samples)

    async def _worker(self) -> None:
        while True:
            samples = await self._queue.get()
            for sample in samples:
                try:
                    await self._callback(sample)
                except Exception:
                    logger.exception("ADS subscription callback failed for '%s'", sample.point_id)


def _normalize_timestamp(value: object) -> tuple[datetime, TimestampSource]:
    """优先采用 pyads 通知时标，无效时才使用本地 UTC 接收时间。"""
    if not isinstance(value, datetime):
        return datetime.now(UTC), "local"
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC), "device"
    return value.astimezone(UTC), "device"
