"""IEC104 主动上送的本地监听注册与分发。

设计边界（与协议原生订阅严格区分）：

- 只做本地注册，不向从站注册任何通知，不轮询、不隐式总召；
- 所有上送样本进入**单一有界队列**，由**单一 worker** 顺序分发，
  避免「每监听者一个永久任务」；
- 队列满时丢最旧样本并计数告警，保证慢消费者不能阻塞底层协议接收；
- 单次回调有**有界执行时间**：超时即取消回调任务，worker 不被
  不可协作的回调无限阻塞；超时后仍未结束的任务仍受注册表跟踪，
  由 ``unlisten``/``close`` 有界等待并显式报告失败；
- 业务回调异常隔离，只记录日志，不影响其他监听者与接收管线。

取消语义（``unlisten`` 契约）：

- 未在执行中的监听立即取消；已入队未分发的样本不再投递给该监听者；
- 正在执行的回调会被请求取消，``unlisten`` 等待其真正结束；
- 回调在规定时间内未结束（吞掉 ``CancelledError`` 或清理阻塞）时，
  ``unlisten`` 显式抛出 ``TimeoutError``，绝不谎报取消成功；
- 重复 ``unlisten`` 幂等；成功返回后保证不存在该监听者的在途回调。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

from core.application.protocol_contract import ProtocolSample

logger = logging.getLogger(__name__)

ProtocolListenerCallback = Callable[[ProtocolSample], Awaitable[None]]

# 单次回调的默认执行时限，同时作为其被取消后的清理等待上限。
DEFAULT_CALLBACK_TIMEOUT = 5.0


def _current_task_cancelling() -> bool:
    """当前任务是否正处于取消传播中（区别于回调任务自身被取消）。"""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _cancel_and_await_task(
    task: asyncio.Task[None], bound: float, timeout_message: str
) -> None:
    """请求取消回调任务并有界等待其真正结束。

    ``shield`` 只隔绝「等待方被取消」，不隔绝内层任务自身被取消：
    回调响应我们的 ``cancel()`` 并以 ``CancelledError`` 结束时，异常会
    透传到这里，这属于**取消成功**，必须与等待方自身被外层取消区分开。

    Raises:
        TimeoutError: 任务在 ``bound`` 内未结束（吞掉取消或清理阻塞）。
        asyncio.CancelledError: 等待方自身被外层取消（继续向上传播）。
    """
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=bound)
    except TimeoutError as exc:
        raise TimeoutError(timeout_message) from exc
    except asyncio.CancelledError:
        # 回调任务已结束：其 CancelledError 是对我们 cancel() 的正常响应。
        # 若当前任务自身也被取消（取消计数>0），或任务竟未结束，
        # 则这是外层取消，必须继续传播。
        if _current_task_cancelling() or not task.done():
            raise


class _Listener:
    """单个监听注册：按 point_id 集合过滤（None = 全部已配置点）。"""

    __slots__ = ("id", "filter", "callback", "removed", "inflight", "finished")

    def __init__(
        self,
        listener_id: int,
        point_filter: frozenset[str] | None,
        callback: ProtocolListenerCallback,
    ) -> None:
        self.id = listener_id
        self.filter = point_filter
        self.callback = callback
        self.removed = False
        # 正在执行的回调任务（None 表示无在途回调）。
        self.inflight: asyncio.Task[None] | None = None
        # 无在途回调时 set；unlisten/close 等待在途回调收敛。
        self.finished = asyncio.Event()
        self.finished.set()


class ListenerRegistry:
    """单 worker、有界队列的监听分发表（仅在所属 asyncio loop 内访问）。"""

    QUEUE_CAPACITY = 1000

    def __init__(
        self,
        *,
        capacity: int | None = None,
        callback_timeout: float = DEFAULT_CALLBACK_TIMEOUT,
    ) -> None:
        if callback_timeout <= 0:
            raise ValueError("callback_timeout must be > 0")
        self._queue: asyncio.Queue[ProtocolSample] = asyncio.Queue(
            maxsize=capacity or self.QUEUE_CAPACITY
        )
        self._callback_timeout = callback_timeout
        self._listeners: dict[int, _Listener] = {}
        self._next_id = 1
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._dropped = 0

    @property
    def dropped_count(self) -> int:
        """因队列满被丢弃的样本总数。"""
        return self._dropped

    @property
    def active(self) -> bool:
        """是否还有未取消的监听者。"""
        return bool(self._listeners)

    def add(
        self,
        point_ids: tuple[str, ...],
        callback: ProtocolListenerCallback,
    ) -> int:
        """登记一个监听者，返回监听 id；空选点表示监听全部已配置点。"""
        if self._closed:
            raise RuntimeError("listener registry is closed")
        listener_id = self._next_id
        self._next_id += 1
        point_filter = frozenset(dict.fromkeys(point_ids)) if point_ids else None
        self._listeners[listener_id] = _Listener(listener_id, point_filter, callback)
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._dispatch(), name="iec104-listen-worker"
            )
        return listener_id

    def offer(self, sample: ProtocolSample) -> None:
        """接收管线入口：入队一条样本；队列满时丢最旧并计数。

        永不阻塞调用方（协议接收任务），也不在无监听者/关闭后排队。
        """
        if self._closed or not self._listeners:
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.warning(
                    "IEC104 listener queue overflow — %d sample(s) dropped "
                    "(slow consumer)",
                    self._dropped,
                )
        self._queue.put_nowait(sample)

    async def remove(self, listener_id: int, *, timeout: float | None = None) -> None:
        """取消监听（幂等），并等待在途回调真正结束。

        Args:
            listener_id: ``add`` 返回的监听 id。
            timeout: 等待回调结束（含取消清理）的上限；None 表示使用
                注册表的 ``callback_timeout``。

        Raises:
            TimeoutError: 回调未在规定时间内结束（吞掉取消或清理阻塞）。
                此时监听注册已摘除，但该回调任务可能仍在事件循环中运行。
        """
        listener = self._listeners.pop(listener_id, None)
        if listener is None:
            return
        listener.removed = True
        await self._await_inflight(listener, timeout or self._callback_timeout)

    async def close(self, *, timeout: float | None = None) -> None:
        """关闭分发表：摘光监听者、取消 worker、取消并收敛全部在途回调、清空队列。

        幂等：重复调用立即返回。任何在途回调都会被请求取消；若回调在
        ``timeout`` 内仍未结束，显式抛出 ``TimeoutError``，不把未完成的
        取消报告为成功。

        Raises:
            TimeoutError: 存在回调在取消后仍未结束。
        """
        if self._closed:
            return
        self._closed = True
        listeners = tuple(self._listeners.values())
        self._listeners.clear()
        for listener in listeners:
            listener.removed = True

        worker, self._worker = self._worker, None
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        # worker 取消只中断分发等待；每个回调包裹任务在此统一收敛。
        bound = timeout or self._callback_timeout
        current = asyncio.current_task()
        inflight = [
            task
            for listener in listeners
            if (task := listener.inflight) is not None
            and not task.done()
            and task is not current
        ]
        for task in inflight:
            task.cancel()
        self._drain_queue()
        stuck: list[asyncio.Task[None]] = []
        for task in inflight:
            try:
                await _cancel_and_await_task(
                    task,
                    bound,
                    timeout_message=(
                        f"IEC104 listener close: callback task did not finish "
                        f"within {bound:.1f}s after cancellation"
                    ),
                )
            except TimeoutError:
                stuck.append(task)
        if stuck:
            raise TimeoutError(
                f"IEC104 listener close: {len(stuck)} callback task(s) did not "
                f"finish within {bound:.1f}s after cancellation"
            )

    async def _dispatch(self) -> None:
        while True:
            sample = await self._queue.get()
            try:
                await self._fan_out(sample)
                # 取消可能在最后一次 wait_for 处被「回调恰好完成」竞态吞掉，
                # fan_out 正常返回后仍必须确认关闭状态，避免 worker 带着
                # 未消费的取消回到 queue.get 永久阻塞。
                if self._closed:
                    raise asyncio.CancelledError
            finally:
                self._queue.task_done()

    async def _fan_out(self, sample: ProtocolSample) -> None:
        # close 先于 worker.cancel() 置位。CancelledError 只投递一次：
        # 若 wait_for 因回调恰好完成而在内部吞掉取消（fut.done 分支），
        # 这里无条件重新确认关闭状态，保证 worker 必能退出而不是回到
        # queue.get 永久等待。
        if self._closed:
            raise asyncio.CancelledError
        # 快照监听者：分发期间被摘除的监听者不再调用。回调顺序执行，
        # 单个慢回调最多延迟其他监听者一个回调时限（绝不无限拖延）。
        for listener in tuple(self._listeners.values()):
            if listener.removed:
                continue
            if listener.filter is not None and sample.point_id not in listener.filter:
                continue
            inflight = listener.inflight
            if inflight is not None and not inflight.done():
                # 上一次回调超时后仍在清理：不对同一监听者并发派发，
                # 跳过本条样本（其他监听者不受影响）。
                logger.debug(
                    "IEC104 listener %s still running a previous callback; "
                    "skipping point '%s'",
                    listener.id,
                    sample.point_id,
                )
                continue
            await self._deliver(listener, sample)

    async def _deliver(self, listener: _Listener, sample: ProtocolSample) -> None:
        """执行单次回调并施加有界超时；任何失败都终止于本方法。"""
        task: asyncio.Task[None] = asyncio.create_task(
            cast(Coroutine[Any, Any, None], listener.callback(sample)),
            name=f"iec104-listen-callback-{listener.id}",
        )
        listener.inflight = task
        listener.finished.clear()
        try:
            # shield：超时后放弃的是「等待」，取消权由本方法显式控制，
            # 避免 wait_for 在不协作回调处无限等待其取消完成。
            await asyncio.wait_for(asyncio.shield(task), timeout=self._callback_timeout)
        except asyncio.CancelledError:
            # _closed 先于 worker.cancel() 置位：即使取消与回调任务自身
            # 结束在 wait_for 内竞态（worker 取消计数可能已被消费），
            # 仍能确定性区分 close 取消与 remove() 取消回调。
            if self._closed or _current_task_cancelling():
                # worker 自身被 close 取消：回调交由 close 收敛。
                task.cancel()
                raise
            # 回调被 remove() 主动取消：本次投递正常收敛。
            self._clear_inflight(listener, task)
        except TimeoutError:
            await self._on_timeout(listener, sample, task)
        except Exception:
            logger.exception(
                "IEC104 listener %s callback failed for point '%s'",
                listener.id,
                sample.point_id,
            )
            self._clear_inflight(listener, task)
        else:
            self._clear_inflight(listener, task)

    async def _on_timeout(
        self,
        listener: _Listener,
        sample: ProtocolSample,
        task: asyncio.Task[None],
    ) -> None:
        """超时后请求取消并给予一个有界清理窗口；仍不结束则保留受跟踪任务。"""
        logger.warning(
            "IEC104 listener %s callback for point '%s' exceeded %.2fs — cancelling",
            listener.id,
            sample.point_id,
            self._callback_timeout,
        )
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._callback_timeout)
        except TimeoutError:
            logger.error(
                "IEC104 listener %s callback for point '%s' still running %.2fs "
                "after cancellation (uncooperative cancellation/cleanup); "
                "further samples to this listener are skipped until it exits",
                listener.id,
                sample.point_id,
                self._callback_timeout,
            )
            task.add_done_callback(self._log_abandoned_task)
            return
        except asyncio.CancelledError:
            if self._closed or _current_task_cancelling():
                raise
        self._clear_inflight(listener, task)

    def _log_abandoned_task(self, task: asyncio.Task[None]) -> None:
        # 失控任务晚结束时仅记录，不允许其异常静默；监听者可能已被注销，
        # 不在这里触碰注册表状态（remove/close 已对结果显式负责）。
        exc: BaseException | None = None
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
        if exc is not None:
            logger.error(
                "IEC104 overdue listener callback ended with an exception: %r",
                exc,
                exc_info=exc,
            )
        else:
            logger.warning("IEC104 overdue listener callback eventually finished")

    async def _await_inflight(self, listener: _Listener, bound: float) -> None:
        """请求取消在途回调并有界等待其真正结束；超时显式失败。"""
        task = listener.inflight
        if task is None or task.done():
            listener.inflight = None
            listener.finished.set()
            return
        await _cancel_and_await_task(
            task,
            bound,
            timeout_message=(
                f"IEC104 listener {listener.id} callback did not finish within "
                f"{bound:.1f}s after cancellation"
            ),
        )
        listener.inflight = None
        listener.finished.set()

    def _clear_inflight(self, listener: _Listener, task: asyncio.Task[None]) -> None:
        if listener.inflight is task:
            listener.inflight = None
            listener.finished.set()

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
