"""IEC104 主动上送的本地监听注册与分发。

设计边界（与协议原生订阅严格区分）：

- 只做本地注册，不向从站注册任何通知，不轮询、不隐式总召；
- 所有上送样本进入**单一有界队列**，由**单一 worker** 顺序分发，
  避免「每监听者一个永久任务」；
- 队列满时丢最旧样本并计数告警，保证慢消费者不能阻塞底层协议接收；
- 单次回调有**有界执行时间**：超时即取消回调任务，worker 不被
  不可协作的回调无限阻塞；超时后仍未结束的任务仍受注册表跟踪，
  由监听句柄的 ``close()`` 有界等待并显式报告失败；
- 业务回调异常隔离，只记录日志，不影响其他监听者与接收管线。

取消语义（监听句柄 ``close()`` 契约）：

- 未在执行中的监听立即取消；已入队未分发的样本不再投递给该监听者；
- 正在执行的回调会被请求取消，``close()`` 等待其真正结束；
- 回调在规定时间内未结束（吞掉 ``CancelledError`` 或清理阻塞）时，
  ``close()`` 显式抛出 ``TimeoutError``，绝不谎报取消成功；
- 重复 ``close()`` 幂等；成功返回后保证不存在该监听者的在途回调。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, cast

from core.application.protocol_contract import ProtocolSample

logger = logging.getLogger(__name__)

ProtocolListenerCallback = Callable[[tuple[ProtocolSample, ...]], Awaitable[None]]

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


def _consume_task_result(task: asyncio.Task[None]) -> None:
    """取回已结束任务的结果，避免未检索异常的 GC 告警。"""
    with contextlib.suppress(asyncio.CancelledError):
        task.exception()


class _Listener:
    """单个监听注册：按 point_id 集合过滤（None = 全部已配置点）。"""

    __slots__ = ("id", "filter", "callback", "removed", "inflight")

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
        # 正在执行的回调任务（None 表示无在途回调）；句柄 close/注册表
        # remove 直接 await 该任务收敛，不再需要额外的完成事件。
        self.inflight: asyncio.Task[None] | None = None


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
        self._queue: asyncio.Queue[tuple[ProtocolSample, ...]] = asyncio.Queue(
            maxsize=capacity or self.QUEUE_CAPACITY
        )
        self._callback_timeout = callback_timeout
        self._listeners: dict[int, _Listener] = {}
        self._next_id = 1
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._dropped = 0
        # close 串行锁：保证资源拆除只执行一次，并发/重复关闭都在锁内
        # 重新等待未收敛任务，而不是仅凭 _closed 提前返回。
        self._close_lock = asyncio.Lock()
        # 已请求取消但尚未结束的回调任务的持久强引用；任务真正结束时
        # 由 done 回调自动摘除。首次 close 超时后这些任务仍受跟踪。
        self._unresolved: set[asyncio.Task[None]] = set()

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

    def offer(self, samples: tuple[ProtocolSample, ...]) -> None:
        """接收管线入口：入队一条样本；队列满时丢最旧并计数。

        永不阻塞调用方（协议接收任务），也不在无监听者/关闭后排队。
        """
        if self._closed or not self._listeners or not samples:
            return
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self._dropped += len(dropped)
            if self._dropped == len(dropped) or self._dropped % 1000 == 0:
                logger.warning(
                    "IEC104 listener queue overflow — %d sample(s) dropped "
                    "(slow consumer)",
                    self._dropped,
                )
        self._queue.put_nowait(samples)

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

        ``_closed`` 只表示**停止接受新监听与新数据**（拆除只执行一次），
        不代表后台资源已全部释放：存在取消后仍未结束的回调时，任务由
        注册表持久跟踪（``_unresolved``），本方法显式抛出 ``TimeoutError``。
        此后重复调用会**继续等待**这些任务：任务结束则正常返回，仍超时
        则再次抛出，绝不把未完成的取消报告为成功。并发关闭由锁串行化，
        资源清理不重复执行；取消其中一个等待方不影响其他调用者收敛。

        Raises:
            TimeoutError: 存在回调在取消后仍未结束。
        """
        async with self._close_lock:
            bound = timeout or self._callback_timeout
            if not self._closed:
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
                current = asyncio.current_task()
                for listener in listeners:
                    task = listener.inflight
                    if task is None or task is current:
                        continue
                    if task.done():
                        self._retire_task(task)
                    else:
                        task.cancel()
                        self._track_task(task)
                self._drain_queue()
            await self._converge(bound)

    async def _converge(self, bound: float) -> None:
        """等待全部受跟踪回调任务结束；仍有任务未结束时显式失败。

        只等待、不重复 ``cancel()``：任务此前已收到取消，重复取消可能
        打断其清理代码。``shield`` 隔绝等待方自身被外层取消。
        """
        stuck: list[asyncio.Task[None]] = []
        for task in tuple(self._unresolved):
            if task.done():
                self._retire_task(task)
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=bound)
            except TimeoutError:
                stuck.append(task)
                continue
            except asyncio.CancelledError:
                # 任务以 CancelledError 结束是对本注册表 cancel() 的正常
                # 响应；仅当等待方自身被外层取消，或任务竟未结束时才传播。
                if _current_task_cancelling() or not task.done():
                    raise
            self._retire_task(task)
        if stuck:
            raise TimeoutError(
                f"IEC104 listener close: {len(stuck)} callback task(s) did not "
                f"finish within {bound:.1f}s after cancellation"
            )

    def _track_task(self, task: asyncio.Task[None]) -> None:
        """持久持有未收敛回调任务的强引用，直到其真正结束。"""
        self._unresolved.add(task)
        task.add_done_callback(self._on_tracked_done)

    def _on_tracked_done(self, task: asyncio.Task[None]) -> None:
        self._unresolved.discard(task)
        _consume_task_result(task)

    def _retire_task(self, task: asyncio.Task[None]) -> None:
        self._unresolved.discard(task)
        _consume_task_result(task)

    async def _dispatch(self) -> None:
        while True:
            samples = await self._queue.get()
            try:
                await self._fan_out(samples)
                # 取消可能在最后一次 wait_for 处被「回调恰好完成」竞态吞掉，
                # fan_out 正常返回后仍必须确认关闭状态，避免 worker 带着
                # 未消费的取消回到 queue.get 永久阻塞。
                if self._closed:
                    raise asyncio.CancelledError
            finally:
                self._queue.task_done()

    async def _fan_out(self, samples: tuple[ProtocolSample, ...]) -> None:
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
            filtered = (
                samples if listener.filter is None
                else tuple(sample for sample in samples if sample.point_id in listener.filter)
            )
            if not filtered:
                continue
            inflight = listener.inflight
            if inflight is not None and not inflight.done():
                # 上一次回调超时后仍在清理：不对同一监听者并发派发，
                # 跳过本条样本（其他监听者不受影响）。
                logger.debug(
                    "IEC104 listener %s still running a previous callback; "
                    "skipping point '%s'",
                    listener.id,
                    filtered[0].point_id,
                )
                continue
            await self._deliver(listener, filtered)

    async def _deliver(self, listener: _Listener, samples: tuple[ProtocolSample, ...]) -> None:
        """执行单次回调并施加有界超时；任何失败都终止于本方法。"""
        task: asyncio.Task[None] = asyncio.create_task(
            cast(Coroutine[Any, Any, None], listener.callback(samples)),
            name=f"iec104-listen-callback-{listener.id}",
        )
        listener.inflight = task
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
            await self._on_timeout(listener, samples[0], task)
        except Exception:
            logger.exception(
                "IEC104 listener %s callback failed for point '%s'",
                listener.id,
                samples[0].point_id,
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

    def _clear_inflight(self, listener: _Listener, task: asyncio.Task[None]) -> None:
        if listener.inflight is task:
            listener.inflight = None

    def _drain_queue(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
