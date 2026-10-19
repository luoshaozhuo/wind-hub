"""一个 ADS 连接的命令串行化、取消收敛和物理关闭。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import pyads  # type: ignore[import-untyped]

from core.application.errors import ProtocolError

logger = logging.getLogger(__name__)
_ADSERR_CLIENT_TIMEOUT = 1861


NotificationKey = tuple[int, int, int, int, float, float]


@dataclass(slots=True)
class _SharedNotification:
    handle: tuple[Any, Any] | None
    callbacks: tuple[Callable[..., None], ...]
    uncertain: bool = False


class _CommandStats:
    """单个 Session 生命周期内的命令计数与耗时诊断。

    所有写入都发生在事件循环线程（``execute`` 调用方与命令 owner task、
    ``aclose`` 关闭管线），因此不需要额外加锁；命令名只有固定的几种 ADS
    命令，基数有界。同步执行的统计由 owner task 在线程真正结束后记录，
    调用方是否取消不影响计数真值（见 :class:`_AdsSession`）。
    """

    __slots__ = (
        "command_counts",
        "wait_count",
        "wait_seconds",
        "wait_max",
        "exec_count",
        "exec_seconds",
        "exec_max",
        "timeouts",
        "rejected",
        "cancelled_while_queued",
        "shutdown_abandoned",
        "shutdown_late_success",
        "shutdown_late_failure",
    )

    def __init__(self) -> None:
        self.command_counts: dict[str, int] = {}
        self.wait_count = 0
        self.wait_seconds = 0.0
        self.wait_max = 0.0
        self.exec_count = 0
        self.exec_seconds = 0.0
        self.exec_max = 0.0
        self.timeouts = 0
        self.rejected = 0
        self.cancelled_while_queued = 0
        self.shutdown_abandoned = 0
        # 异步预算耗尽后底层 close 线程迟到结束的结局计数。
        self.shutdown_late_success = 0
        self.shutdown_late_failure = 0

    def record_gate_wait(self, name: str, seconds: float) -> None:
        """记录一次异步命令门排队等待（含零等待）。"""
        self.command_counts[name] = self.command_counts.get(name, 0) + 1
        self.wait_count += 1
        self.wait_seconds += seconds
        if seconds > self.wait_max:
            self.wait_max = seconds

    def record_execution(self, seconds: float, *, timeout: bool = False) -> None:
        """记录一次同步命令的实际执行时长（owner 在线程结束后调用）。"""
        self.exec_count += 1
        self.exec_seconds += seconds
        if seconds > self.exec_max:
            self.exec_max = seconds
        if timeout:
            self.timeouts += 1

    def record_rejected(self) -> None:
        self.rejected += 1

    def record_queued_cancel(self) -> None:
        """命令仍在门后排队即被取消：从未进入线程池，不计执行数。"""
        self.cancelled_while_queued += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "commands": dict(self.command_counts),
            "gate_wait": {
                "count": self.wait_count,
                "total_seconds": round(self.wait_seconds, 6),
                "max_seconds": round(self.wait_max, 6),
            },
            "execution": {
                "count": self.exec_count,
                "total_seconds": round(self.exec_seconds, 6),
                "max_seconds": round(self.exec_max, 6),
            },
            "timeouts": self.timeouts,
            "rejected_after_closing": self.rejected,
            "cancelled_while_queued": self.cancelled_while_queued,
            "shutdown_abandoned": self.shutdown_abandoned,
            "shutdown_late_success": self.shutdown_late_success,
            "shutdown_late_failure": self.shutdown_late_failure,
        }


def _is_ads_client_timeout(exc: BaseException) -> bool:
    """pyads 客户端同步等待超时（错误码 1861）。"""
    ads_error = getattr(pyads, "ADSError", None)
    return (
        ads_error is not None
        and isinstance(exc, ads_error)
        and getattr(exc, "err_code", None) == _ADSERR_CLIENT_TIMEOUT
    )


class _AdsSession:
    """一条 pyads 连接的两级命令闸门。

    A 组并发矩阵已证实：同一 pyads Connection 上多线程并发执行同步命令，
    会触发 AdsLib 本地 AMS 端口/请求槽竞争（``Port: 30001 already in
    use``、``ADSError(-1)``），失败发生在客户端本地，多数命令根本不上线；
    实验中没有观测到 AMS 请求/响应帧错配（0 mismatch），但也无法严格证伪
    （竞争命令未上线）。因此同一 Session 的所有同步 ADS 命令必须严格串行。

    串行化分两层，缺一不可：

    - ``execute`` 外层的异步门 ``_gate``：命令先在 asyncio 层排队，批量
      Sum Read/Write 的分块之间会释放该门，允许 health/控制写插入；
    - ``run`` 内的线程锁 ``_cmd_lock``：串行实际 pyads 调用，并保证
      ``shutdown`` 不会在命令在途时关闭连接。

    **门的所有权在取得后转交给命令 owner task。** 调用方在门后排队
    （可取消，取消即永不上线）；取得门后创建短命的 owner task 执行
    ``to_thread``，调用方通过 ``asyncio.shield`` 等待。调用方随后被
    取消时 owner 不会取消：同步命令（可能已经写向 PLC）必须跑完且不
    重发，门也必须持有到工作线程真正退出——否则后续命令会提前进入线程
    池、空耗一个工作线程阻塞在 ``_cmd_lock`` 上。owner 完成后由其自身
    释放门并记录真实执行统计；owner 集合（``_owners``）保证强引用，
    done 回调消费异常，杜绝无人观察的 Task 异常。这不是驻留 Worker：
    owner 每命令一个，结束即回收，Session 不做任何排队调度。

    ``shutdown`` 必须获取 ``_cmd_lock`` 后才关闭端口，因此：

    - 关闭期间不可能有旧命令线程仍在使用该连接（无 use-after-close）；
    - 外层 ``asyncio`` 超时/取消后迟到返回的旧线程不可能关闭新会话；
    - ``shutdown`` 的等待上界 = 在途命令的 pyads 原生超时 + grace。

    获取锁超过 grace 仍失败、或 ``connection.close()`` 抛错时放弃关闭
    该端口（显式记录、返回 False），绝不无限等待、绝不谎称关闭成功。

    **异步等待超时不等于物理关闭结束。** ``connection.close()`` 一旦在线程
    内开始执行，就不能被 Python 安全取消：管线超时只结束 asyncio 侧等待，
    物理 close 线程由独立的、永不取消的 owner task 继续追踪，结果/异常
    收敛到 ``_shutdown_settled`` future。物理 close 在途期间
    :attr:`physical_close_pending` 为真；Driver 的路由级协调器
    （:class:`_RouteState`）从摘除会话前就持有路由变更锁，直到该 future
    收敛才释放，因此同路由新连接不可能与迟到的 adsDelRoute 交错。迟到结束
    时记录成功/失败结局，绝不补第二次 close。
    """

    def __init__(
        self, connection: Any, label: str = "", *, max_notifications: int = 550
    ) -> None:
        self._connection = connection
        # 设备标签仅用于诊断日志；空标签用于单元测试替身。
        self._label = f"[{label}] " if label else ""
        self._cmd_lock = threading.Lock()
        # 异步命令门：同一 Session 至多一条命令获得 to_thread 执行资格。
        # 门由命令 owner task 持有到同步线程真正结束（见类文档）。
        # asyncio.Lock 在 3.11 首次使用时绑定循环，Driver 通常在循环外构造。
        self._gate = asyncio.Lock()
        self._closing = False
        self._stats = _CommandStats()
        self._max_notifications = max_notifications
        self._notification_lock = asyncio.Lock()
        self._notifications: dict[NotificationKey, _SharedNotification] = {}
        # 在途 owner task 强引用集合；done 回调移除并消费其异常。
        self._owners: set[asyncio.Task[Any]] = set()
        # aclose 幂等收敛到同一个关闭管线 task（调用方取消不中断清理）。
        self._close_task: asyncio.Task[bool] | None = None
        # 物理 close（to_thread shutdown）的受追踪 owner：管线超时后仍保留，
        # 永不取消；None 表示尚未提交物理关闭。
        self._shutdown_task: asyncio.Task[bool] | None = None
        # 物理 close 线程真实结束（成功/失败/异常）后收敛的 future。
        self._shutdown_settled: asyncio.Future[bool] | None = None
        # 异步管线已超时放弃、但物理 close 线程仍在途。
        self._shutdown_abandoned = False
        self._shutdown_started = 0.0
        # AdsLib 路由键（目标 Net ID，缺省主机地址）；由 Driver 在创建时
        # 设置；未挂载到 Driver 的测试替身为空串，不参与跨 Driver 隔离。
        self.route_key = ""

    @property
    def connection(self) -> Any:
        return self._connection

    @property
    def is_open(self) -> bool:
        return bool(self._connection.is_open)

    @property
    def stats(self) -> _CommandStats:
        return self._stats

    @property
    def physical_close_pending(self) -> bool:
        """物理 close 线程已提交但尚未确认结束（成功或失败均算结束）。

        为真时旧 Session 占用的 AdsLib 路由状态未知，同路由不得建立新连接。
        """
        future = self._shutdown_settled
        return future is not None and not future.done()

    @property
    def shutdown_settled_future(self) -> asyncio.Future[bool] | None:
        """物理 close 收敛 future；未提交过物理关闭时为 None。"""
        return self._shutdown_settled

    async def wait_close_settled(self, timeout: float) -> bool:
        """有界等待物理 close 线程真实结束；超时返回 False，不取消底层关闭。

        物理 close 以异常结束同样视为"已收敛"（线程确已退出，结果可观察）。
        调用方取消只中断本次等待，不转移底层所有权（future 永不被取消）。
        """
        future = self._shutdown_settled
        if future is None or future.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return False
        except BaseException:
            # future 以异常收敛：物理线程已结束，异常由 owner 回调统一记录。
            return True
        return True

    def notification(self, datatype: object) -> Callable[..., Callable[..., None]]:
        return cast(
            Callable[..., Callable[..., None]],
            self._connection.notification(datatype),
        )

    async def acquire_notification(
        self,
        key: NotificationKey,
        attribute: object,
        wrapped: Callable[..., None],
        datatype: object,
    ) -> None:
        """等价 Notification 共享一个物理 Handle；Session 即代际边界。"""
        async with self._notification_lock:
            entry = self._notifications.get(key)
            if entry is not None:
                if entry.uncertain:
                    raise ProtocolError("ADS notification registration is unconfirmed")
                entry.callbacks = (*entry.callbacks, wrapped)
                return

            if len(self._notifications) >= self._max_notifications:
                raise ProtocolError(
                    f"ADS physical notification limit {self._max_notifications} reached"
                )

            # 只读取不可变回调快照，允许 pyads 的外部线程并发调用。
            def dispatch(*args: Any) -> None:
                current = self._notifications.get(key)
                if current is None:
                    return
                for listener in current.callbacks:
                    try:
                        listener(*args)
                    except Exception:
                        logger.exception(
                            "%sADS shared notification listener failed for %s",
                            self._label,
                            key[:2],
                        )

            # 先建立接收入口，再提交注册，避免设备立即通知时丢掉首批数据。
            entry = _SharedNotification(None, (wrapped,))
            self._notifications[key] = entry
            try:
                callback = self.notification(datatype)(dispatch)
            except Exception:
                self._notifications.pop(key, None)
                raise
            # 实际命令收敛必须独立于注册调用方的取消；只有收到真实结果
            # 才能把 Handle 所有权登记给逻辑订阅。
            register = asyncio.create_task(
                self.execute("add_device_notification", key[:2], attribute, callback),
                name="ads-physical-notification-register",
            )
            try:
                entry.handle = cast(tuple[Any, Any], await asyncio.shield(register))
            except asyncio.CancelledError:
                # 调用方取消后，仍必须接住底层 ADD 的结果并尝试释放。
                # 无法确认 DEL 时保留隔离条目，不允许下一次重复注册。
                entry.callbacks = ()
                try:
                    entry.handle = cast(tuple[Any, Any], await asyncio.shield(register))
                    await self.execute(
                        "del_device_notification", entry.handle[0], entry.handle[1]
                    )
                except Exception:
                    entry.uncertain = True
                else:
                    self._notifications.pop(key, None)
                raise
            except Exception:
                entry.callbacks = ()
                entry.uncertain = True
                raise

    async def release_notification(
        self, key: NotificationKey, wrapped: Callable[..., None]
    ) -> None:
        """只有最后一个有效引用退出，才真正注销物理 Notification。"""
        async with self._notification_lock:
            entry = self._notifications.get(key)
            if entry is None:
                return
            entry.callbacks = tuple(
                callback for callback in entry.callbacks if callback is not wrapped
            )
            if entry.callbacks:
                return
            if entry.uncertain:
                raise ProtocolError("ADS notification deregistration remains unconfirmed")
            if entry.handle is None:
                raise ProtocolError("ADS notification registration has not settled")
            try:
                await self.execute(
                    "del_device_notification", entry.handle[0], entry.handle[1]
                )
            except Exception:
                entry.uncertain = True
                raise
            self._notifications.pop(key, None)

    async def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """统一的异步命令入口：异步门排队 → owner 持门到线程结束。

        - 在门后排队期间被取消：命令从未进入线程池，记录
          ``cancelled_while_queued``，不上线、不计数执行；
        - 取得门后若 Session 正在关闭：快速拒绝并立即释放门；
        - owner 启动后调用方被取消：owner 继续把唯一一次同步命令跑完
          （不重发），门与统计都以线程真实结束为准；
        - 成功/异常/1861 超时的执行统计由 owner 在线程结束后记录，
          与调用方是否仍在等待无关。
        """
        loop = asyncio.get_running_loop()
        queued_at = loop.time()
        try:
            await self._gate.acquire()
        except asyncio.CancelledError:
            # 等待门期间取消：门未取得（asyncio 授予后取消会落在后续
            # await，即 shield 处而非此处），命令不可能上线。
            self._stats.record_queued_cancel()
            raise
        # 门已取得；自此到 owner 创建之间没有 await，单线程内不会被
        # 取消打断，门的所有权随即转交给 owner。
        self._stats.record_gate_wait(name, loop.time() - queued_at)
        if self._closing:
            self._gate.release()
            self._stats.record_rejected()
            raise ProtocolError(f"ADS session is closing; command '{name}' rejected")
        owner = loop.create_task(
            self._run_owned(name, args, kwargs, loop.time()),
            name=f"ads-command-{name}",
        )
        self._owners.add(owner)
        owner.add_done_callback(self._owner_done)
        # shield：调用方取消只中断等待，不取消 owner；owner 永不被取消。
        return await asyncio.shield(owner)

    async def _run_owned(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        started: float,
    ) -> Any:
        """owner task：持门执行同步命令，线程真正结束后才释放门并记账。"""
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.to_thread(self.run, name, *args, **kwargs)
        except BaseException as exc:
            timed_out = _is_ads_client_timeout(exc)
            # 记账点在 to_thread 返回之后：调用方已取消时，记录的仍是
            # 同步命令的真实耗时与真实结局，不会把取消误记成提前完成。
            self._stats.record_execution(loop.time() - started, timeout=timed_out)
            if timed_out:
                # 客户端 1861 超时是连接级风险信号（后续会话恢复依赖
                # 上层断线处理），低频且可操作：带设备/命令/状态记录。
                logger.warning(
                    "%sADS command '%s' timed out (ADSError 1861); session_closing=%s",
                    self._label,
                    name,
                    self._closing,
                )
            raise
        else:
            self._stats.record_execution(loop.time() - started)
            return result
        finally:
            # 释放点是"同步线程已退出 run"，早于此点发放的下一个资格
            # 都会在线程池里空等 _cmd_lock。
            self._gate.release()

    def _owner_done(self, task: asyncio.Task[Any]) -> None:
        """回收 owner 强引用并消费异常（调用方可能已取消，无人再 await）。"""
        self._owners.discard(task)
        if not task.cancelled():
            task.exception()

    def run(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """在工作线程内执行一条 pyads 同步命令（``_cmd_lock`` 串行）。"""
        with self._cmd_lock:
            if self._closing:
                raise ProtocolError(f"ADS session is closing; command '{name}' rejected")
            return getattr(self._connection, name)(*args, **kwargs)

    async def aclose(self, grace: float) -> bool:
        """异步关闭会话：拒绝新命令，等待在途 owner 结束后关闭端口。

        幂等且不受调用方取消影响：首次调用同步置位 ``_closing`` 并启动
        关闭管线，重复/并发调用与取消后的再次 await 收敛到同一 task。
        管线失败（放弃/异常）返回 False，端口状态不谎报为已关闭；管线超时
        放弃后物理 close 线程迟到收敛的，再次调用以收敛 future 的真实结果
        为准（迟到成功返回 True），不重复提交物理关闭。
        """
        settled = self._shutdown_settled
        if settled is not None and settled.done():
            # 建链流程内已通过 shutdown_inline 在当前 task 完成物理关闭；
            # 后续 aclose 直接复用其结局，绝不补发第二次 close。
            if settled.exception() is not None:
                return False
            return bool(settled.result())
        if self._close_task is None:
            # 必须在首个 await 前同步置位：此后取得门的新命令一律拒绝。
            self._closing = True
            self._close_task = asyncio.create_task(
                self._close_pipeline(grace), name="ads-session-close"
            )
        result = await asyncio.shield(self._close_task)
        if result:
            return True
        settled = self._shutdown_settled
        if settled is not None and settled.done():
            # 管线返回 False 的另一条路径（等门超时等）下 future 可能尚未
            # 产生；仅在物理 close 确已收敛时才以其结果为准。
            exc = settled.exception()
            if exc is None:
                return bool(settled.result())
        return False

    async def _close_pipeline(self, grace: float) -> bool:
        """关闭总预算 = grace + 2s，覆盖"等门排空"与"shutdown 线程"两段。"""
        loop = asyncio.get_running_loop()
        budget = grace + 2.0
        deadline = loop.time() + budget
        # 第一段：等待门。门由在途 owner 在线程真正结束后释放，门后排队
        # 的命令看到 _closing 会自行拒绝并放门（FIFO 保证管线最终取得）。
        try:
            await asyncio.wait_for(self._gate.acquire(), timeout=budget)
        except TimeoutError:
            self._stats.shutdown_abandoned += 1
            logger.error(
                "%sADS session shutdown abandoned: command gate did not drain within "
                "%.3fs (%d command owner(s) still running); port left open (no close issued)",
                self._label,
                budget,
                len(self._owners),
            )
            return False
        try:
            remaining = deadline - loop.time()
            shutdown_grace = min(grace, max(remaining, 0.0))
            # 物理 close 提交给独立的、永不取消的 owner：管线超时/调用方取消
            # 都只结束本次等待，底层 connection.close() 的生命周期与结果
            # 由 owner + _shutdown_settled 继续追踪（见类文档）。
            self._shutdown_started = loop.time()
            self._shutdown_settled = loop.create_future()
            self._shutdown_task = loop.create_task(
                self._shutdown_owned(shutdown_grace, self._shutdown_settled),
                name="ads-session-physical-close",
            )
            self._owners.add(self._shutdown_task)
            self._shutdown_task.add_done_callback(self._owner_done)
            try:
                closed = await asyncio.wait_for(
                    asyncio.shield(self._shutdown_task), timeout=remaining
                )
            except TimeoutError:
                # asyncio 等待预算耗尽绝不等于物理 close 停止：connection.close()
                # 可能已在工作线程内执行（Linux 上先 adsDelRoute 再关私有端口），
                # 线程不可杀、close 不可重发。标记放弃并由 owner 继续收敛；
                # Driver 的路由变更锁由 _shutdown_settled 收敛回调释放，
                # 同路由新连接在此之前无法建立。
                self._shutdown_abandoned = True
                self._stats.shutdown_abandoned += 1
                logger.error(
                    "%sADS session shutdown wait budget %.3fs exhausted while physical "
                    "close thread is still running (route_key=%s); the thread remains "
                    "tracked, reconnects to the same route are blocked until it converges, "
                    "and no second close will be issued",
                    self._label,
                    budget,
                    self.route_key or "-",
                )
                return False
        finally:
            self._gate.release()
        if not closed:
            self._stats.shutdown_abandoned += 1
            logger.error("%sADS session shutdown did not close; port left open", self._label)
        return closed

    async def _shutdown_owned(self, grace: float, settled: asyncio.Future[bool]) -> bool:
        """物理 close owner：永不被取消，唯一负责 connection.close() 的结局。

        线程真实结束后（成功或异常）收敛 ``settled``；若管线此前已因预算
        耗尽放弃，此处记录迟到成功/失败。Driver 监听该 future 释放路由变更
        锁并摘除 closing 登记，被挡住的同路由新连接随后才能建立。
        """
        loop = asyncio.get_running_loop()
        try:
            closed = await asyncio.to_thread(self.shutdown, grace)
        except BaseException:
            # shutdown 自身不主动抛错（内部已归一化），这里兜底防止异常
            # 让 settled 永远 pending 而堵死后续 connect。
            logger.exception(
                "%sADS physical close thread raised unexpectedly",
                self._label,
            )
            closed = False
        elapsed = loop.time() - self._shutdown_started
        abandoned = self._shutdown_abandoned
        if abandoned:
            if closed:
                self._stats.shutdown_late_success += 1
                logger.warning(
                    "%sADS physical close succeeded late after %.3fs; route/port now released",
                    self._label,
                    elapsed,
                )
            else:
                self._stats.shutdown_late_failure += 1
                logger.error(
                    "%sADS physical close finished late with failure after %.3fs; "
                    "port state left unconfirmed",
                    self._label,
                    elapsed,
                )
        if not settled.done():
            settled.set_result(closed)
        return closed

    async def shutdown_inline(self, grace: float) -> bool:
        """在调用方 task 内直接执行物理关闭（仅用于建链流程中的全新会话）。

        与正常关闭管线的区别：调用方（``ADSDriver._connect_locked``）已持有
        该路由的变更锁，物理关闭绝不能再经独立 owner task 去申请同一把锁
        （否则自锁）。这些 Session 从未发布给业务路径（``_connected`` 为
        假），在途命令最多是建链自身 await 过的 health/符号查询，不存在仍在
        线程池内的命令。结局同样收敛到 ``_shutdown_settled``：之后再调用
        :meth:`aclose` 复用该结果，不重复关闭。
        """
        loop = asyncio.get_running_loop()
        self._closing = True
        self._shutdown_started = loop.time()
        self._shutdown_settled = loop.create_future()
        closed = await asyncio.to_thread(self.shutdown, grace)
        if not self._shutdown_settled.done():
            self._shutdown_settled.set_result(closed)
        if not closed:
            self._stats.shutdown_abandoned += 1
        return closed

    def shutdown(self, grace: float) -> bool:
        """等待在途命令线程结束后关闭连接；失败返回 False，不吞异常语义。

        - 抢不到 ``_cmd_lock``（仍有同步线程在途）：超时放弃，不发出
          close，返回 False；
        - ``connection.close()`` 抛异常：记录设备标识与异常堆栈，连接
          状态不可确认，返回 False（绝不记为成功关闭）。
        """
        if not self._cmd_lock.acquire(timeout=grace):
            logger.error(
                "%sADS session shutdown gave up after %.3fs: a command thread is "
                "still running; port left open (no close issued)",
                self._label,
                grace,
            )
            return False
        try:
            self._closing = True
            try:
                self._connection.close()
            except Exception:
                logger.exception("%sADS connection close failed", self._label)
                return False
        finally:
            self._cmd_lock.release()
        return True


