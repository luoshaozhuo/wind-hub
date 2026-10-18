"""基于 pyads 的共享 ADS ProtocolPort Adapter。

Driver 实现共享的 connect/read/write/health，并通过可选订阅能力暴露 ADS
device-notification。何时订阅、订阅哪些点以及失败后的应用级重建策略仍由上层决定。
"""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from ctypes import Structure, c_uint32
from datetime import UTC, datetime
from typing import Any

import pyads  # type: ignore[import-untyped]
from pyads.constants import ADSIGRP_SUMUP_READ, ADSIGRP_SUMUP_WRITE  # type: ignore[import-untyped]

from core.application.errors import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.port.protocol import ListenHandle
from core.application.protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WriteOutcome,
)
from core.application.settings import ADSLocalConfig
from core.domain import ConnectionEndpoint, PointTable, ProtocolOptions

from .codec import (
    compile_decoder,
    _as_point_scalar,
    _bad_sample,
    _coerce_write_value,
    _decode_sum_write_errors,
    _encode_value,
    _is_point_level_error,
    _plc_datatype,
)
from .config import ADSConfig, parse_ads_config
from .mapping import ADSPoint, parse_ads_point, symbol_index_address
from .route import (
    _RouteState,
    _ams_barrier,
    _ams_barrier_closing,
    _registry,
    _route_state,
)
from .session import _AdsSession
from .subscription import ADSSubscription

logger = logging.getLogger(__name__)
_REMOTE_ROUTE_LOCK = threading.Lock()
_LOCAL_AMS_LOCK = threading.RLock()
_LOCAL_AMS: ADSLocalConfig | None = None
_LOCAL_PORT_OPEN = False


def _ads_state_name(value: object) -> str:
    """把 pyads ADSSTATE_* 数值转成可诊断名称；未知值保留原值。"""
    constants = getattr(pyads, "constants", None)
    if constants is not None:
        for name in dir(constants):
            if name.startswith("ADSSTATE_") and getattr(constants, name, object()) == value:
                return name.removeprefix("ADSSTATE_")
    return str(value)


class ADSDriver:
    """单个 Endpoint 的 ADS Driver。

    symbol 点在每次 ADS session 建立后解析为 index_group/index_offset 并缓存；
    read/write 不在周期路径上重复查询 symbol。
    """

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        if point_table.protocol.name != "ads":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'ads'"
            )

        self._point_table_id = point_table.point_table_id
        self._config: ADSConfig = parse_ads_config(
            endpoint,
            protocol_options,
        )
        # 设备标签：只用于诊断日志（错误/统计/丢弃），不进 metrics 标签。
        self._label = f"{self._config.host}:{self._config.target_port}"
        # AdsLib 路由隔离键：pyads Connection.close() 在 Linux 上先
        # adsDelRoute(目标 Net ID)——该路由进程级共享且不按连接引用计数，
        # 迟到 close 会删掉同目标新连接的路由。未配置 Net ID 时用主机地址
        # （此时旧 close 的 adsDelRoute 针对自动探测的同主机 Net ID）。
        self._route_key = self._config.target_net_id or self._config.host

        self._lock = asyncio.Lock()
        # connect 与 close 互斥：旧会话的路由/端口关闭必须完整结束后，
        # 新会话才能建立，避免 adsDelRoute 影响共享同一 Net ID 的新连接。
        self._lifecycle = asyncio.Lock()
        self._session: _AdsSession | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._connected = False
        self._subscriptions: set[ADSSubscription] = set()
        # 注册进行中（可能因取消而未进入 _subscriptions）的订阅；
        # 其后台清理必须被 driver.close 等待，避免晚到 Handle 无人注销。
        self._starting_subscriptions: set[ADSSubscription] = set()
        # Symbol 解析结果仅对当前 ADS session 有效。
        self._symbol_cache: dict[str, tuple[int, int, int | None]] = {}
        # 缓存预编译的 Sum Read 请求及其逐点解码器。
        self._read_cache: dict[
            tuple[ADSPoint, ...],
            tuple[
                tuple[Any, ...],
                tuple[Callable[[bytes | memoryview, int], PointScalar], ...],
            ],
        ] = {}
        self._points: dict[str, ADSPoint] = {}
        self.update_point_table(point_table)

    def update_point_table(self, point_table: PointTable) -> None:
        """热重载点表：重建符号/类型映射并失效读取缓存（不断开连接）。

        进行中的 ADS 订阅仍按旧 symbol 通知，由上层重启订阅后按新映射
        重新注册。
        """
        # update_point_table 是同步方法，无法在这里异步查询 PLC Symbol。
        # 对于在线 Symbol 表变更，要求通过 reconnect 重新初始化会话地址。
        if self._connected and any(
            point.ext.get("symbol") is not None for point in point_table.points.values()
        ):
            raise ConfigError("ADS symbol point table update requires reconnect")
        self._point_table_id = point_table.point_table_id
        self._points = {
            point.point_id: parse_ads_point(point) for point in point_table.points.values()
        }
        self._read_cache.clear()
        self._symbol_cache.clear()

    def capabilities(self) -> frozenset[ProtocolCapability]:
        """返回 ADS Driver 实际支持的协议能力。"""
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.WRITE_MANY,
                ProtocolCapability.SUBSCRIBE,
            }
        )

    @staticmethod
    def _initialize_local_sync(config: ADSLocalConfig) -> None:
        """进程级 AMS 身份只初始化一次，与具体设备会话无关。"""
        global _LOCAL_AMS, _LOCAL_PORT_OPEN
        with _LOCAL_AMS_LOCK:
            if _LOCAL_AMS is not None:
                if (
                    _LOCAL_AMS.local_ams_net_id != config.local_ams_net_id
                    or _LOCAL_AMS.local_ip != config.local_ip
                ):
                    raise ConfigError("ADS local AMS identity conflicts with initialized identity")
                _LOCAL_AMS = config
                return
            if sys.platform.startswith("linux"):
                pyads.open_port()
                try:
                    pyads.set_local_address(config.local_ams_net_id)
                except BaseException:
                    pyads.close_port()
                    raise
                _LOCAL_PORT_OPEN = True
            _LOCAL_AMS = config

    @classmethod
    async def initialize_local(cls, config: ADSLocalConfig) -> None:
        # 与 close_local() 互斥：复位闩锁与打开进程端口在同一闸内完成，
        # 不会与拆除或建链登记交错。
        registry = _registry()
        async with registry.barrier:
            await asyncio.to_thread(cls._initialize_local_sync, config)
            registry.local_closed = False

    @classmethod
    async def close_local(cls) -> None:
        """释放进程级本地 AMS 端口；不得在其他 ADS Driver 工作期间调用。

        上层生命周期契约（collector/commander assembly）：``runtime.stop()``
        等全部 Driver close 收敛后才调用本方法。直接调用时这里提供可验证的
        安全边界：在进程级闸门内原子地「检查 + 拆除」，任一路由仍存在活动
        Driver、建链中登记或物理 close 未收敛的 Session 时显式拒绝，绝不边
        拆边让新的路由操作进入。不代为无限等待。
        """
        global _LOCAL_AMS, _LOCAL_PORT_OPEN
        registry = _registry()
        async with registry.barrier:
            occupied = sorted(
                key for key, state in registry.routes.items() if state.active or state.closing or state.unsafe_reason is not None
            )
            if occupied:
                raise ProtocolError(
                    "ADS local port cannot be closed while ADS sessions are "
                    f"active or physical close threads are converging: routes={occupied}"
                )
            with _LOCAL_AMS_LOCK:
                if _LOCAL_PORT_OPEN:
                    await asyncio.to_thread(pyads.close_port)
                _LOCAL_PORT_OPEN = False
                _LOCAL_AMS = None
            # 闩锁留在真位：此后建链快速失败，直到再次 initialize_local。
            registry.local_closed = True

    def _new_connection(self) -> Any:
        """创建配置一致、尚未 open 的 pyads Connection。"""
        connection = pyads.Connection(
            self._config.target_net_id or None,
            self._config.target_port,
            self._config.host,
        )
        return connection

    async def connect(self) -> None:
        """连接并检查 PLC RUN 状态；失败时可添加一次反向路由再重连。

        与 :meth:`close` 经 ``_lifecycle`` 互斥，并在路由级变更锁
        （:class:`_RouteState`）保护下完成：旧会话的 adsDelRoute 物理收敛
        前不会执行新会话的 adsAddRoute，同 Net ID 的其他活动 Driver 被显式
        拒绝（AdsLib 路由不按连接引用计数，不支持多活动 Session）。
        """
        async with self._lifecycle:
            await self._connect_locked()

    def _reject_duplicate_route_user(self, route: _RouteState) -> None:
        """同一路由只允许一个活动 Driver：第二个显式拒绝而非依赖时序侥幸。"""
        others = [driver for driver in route.active if driver is not self]
        if others:
            labels = ", ".join(sorted({driver._label for driver in others}))
            raise ConfigError(
                f"ADS route {self._route_key!r} is already in use by another active "
                f"device session ({labels}); a target AMS Net ID route is process-global "
                "and cannot be shared by concurrent connections"
            )

    async def _connect_locked(self) -> None:
        # 锁序恒为「路由变更锁 → Driver 状态锁 → Session 门」，close 路径
        # 同序（_close_all 先取路由锁再摘会话），不存在反向获取。建链是生命
        # 周期操作（罕见且需要与 close/IO 明确互斥）：全程持 Driver 状态锁，
        # 阻止 IO 路径快照到半建立的会话。
        route = _route_state(self._route_key)
        route_timeout = max(self._config.timeout, 5.0) + 1.0
        try:
            # 取得路由锁即完成"准入"：锁未释放期间，任何物理 close 的
            # adsDelRoute 都无法启动；持锁者若在等待，只可能是同路由旧物理
            # close 尚未收敛（有界等待，超时明确报告恢复受阻，不强行建链）。
            await asyncio.wait_for(route.op_lock.acquire(), timeout=route_timeout)
        except TimeoutError:
            raise ProtocolError(
                f"ADS connect to route {self._route_key} blocked: a previous session "
                f"physical close has not converged within {route_timeout:.3f}s; "
                "route state unsafe for new connections"
            ) from None
        try:
            async with self._lock:
                if self._connected and self._session is not None:
                    return
                if self._session is not None:
                    # 上一轮建链失败残留、从未发布的会话：持锁当场清理，
                    # 其物理关闭（adsDelRoute）必须先于本轮 adsAddRoute。
                    stale = self._session
                    self._session = None
                    await self._shutdown_session_inline(stale, route)
                last_health: ConnectionHealth | None = None

                # 准入检查：持路由锁期间其他 Driver 的物理关闭无法启动
                # （它们先要取得同一把锁），因此"检查 → open"之间占用状态
                # 不可能变化，不存在 TOCTOU 窗口。ConfigError 必须在建链
                # 重试循环之外直接抛出。
                async with _ams_barrier():
                    self._reject_duplicate_route_user(route)
                    if route.unsafe_reason is not None:
                        raise ProtocolError(
                            f"ADS route {route.key!r} quarantined: {route.unsafe_reason}"
                        )

                for attempt in range(2 if self._config.route_auto_add else 1):
                    session = _AdsSession(self._new_connection(), self._label)
                    session.route_key = self._route_key
                    connection = session.connection
                    self._session = session
                    try:
                        # open() 内含进程级 adsAddRoute：闩锁检查、活动登记
                        # 与底层 open 在同一进程闸门内完成，close_local 的
                        # 「检查+拆除」无法插入这个窗口。
                        async with _ams_barrier():
                            await asyncio.to_thread(connection.open)
                            if not connection.is_open:
                                raise ConnectionError("ADS session did not open")
                            # 成功 open 即占用路由，直到物理关闭收敛才释放，
                            # 覆盖「摘除后、adsDelRoute 前」窗口。
                            route.active.add(self)
                        await asyncio.to_thread(
                            connection.set_timeout, int(self._config.timeout * 1000)
                        )
                        last_health = await self._read_health(session)
                        if last_health.healthy:
                            self._invalidate_symbol_addresses()
                            await self._resolve_configured_points()
                            self._connected = True
                            return
                    except ProtocolError:
                        # 进程闸门闩锁（close_local 后）等显式失败不重试。
                        self._session = None
                        await self._shutdown_session_inline(session, route)
                        raise
                    except Exception:
                        last_health = ConnectionHealth(
                            healthy=False,
                            message=f"ADS connection failed for {self._config.host}",
                        )
                    finally:
                        if not self._connected and self._session is session:
                            self._session = None
                            await self._shutdown_session_inline(session, route)

                    if attempt == 0 and self._config.route_auto_add:
                        identity = _LOCAL_AMS
                        if identity is None:
                            raise ConfigError("ADS local AMS identity is not initialized")
                        try:
                            # 路由是 PLC 持久资源；pyads.delete_route() 只删本地路由。
                            def add_route(identity: ADSLocalConfig) -> bool:
                                with _REMOTE_ROUTE_LOCK:
                                    return bool(
                                        pyads.add_route_to_plc(
                                            identity.local_ams_net_id,
                                            identity.local_ip,
                                            self._config.host,
                                            self._config.username,
                                            self._config.password,
                                        )
                                    )

                            added = await asyncio.to_thread(add_route, identity)
                            if not added:
                                raise RuntimeError("remote route registration refused")
                        except Exception:
                            raise ProtocolError(
                                f"ADS remote route registration failed for {self._config.host}"
                            ) from None

            raise ProtocolError(
                f"ADS connect failed for {self._config.host}: "
                f"{last_health.message if last_health else 'unknown failure'}"
            )
        finally:
            route.op_lock.release()

    async def close(self) -> None:
        """关闭当前 ADS session；重复/并发调用安全且有界。

        关闭任务独立于调用方协程：调用方被取消时清理仍会执行完毕，
        connect 经 ``_lifecycle`` 等待关闭完全结束后才允许新会话。
        """
        task = self._close_task
        if task is None or task.done():
            # 生命周期锁必须在 close() 首次让出事件循环之前取得：调用方常按
            # create_task(close()); create_task(connect()) 紧接排队，connect
            # 协程此刻已在就绪队列中。无竞争路径的 acquire() 不会让出循环，
            # 因此随后被调度的 connect 只能在锁外等待；实际清理仍放入独立
            # task，使调用方取消不会中断清理。
            await self._lifecycle.acquire()
            task = asyncio.create_task(self._close_all(), name="ads-driver-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close_all(self) -> None:
        # 生命周期锁由 close() 在入队前取得，这里负责释放（asyncio 锁不绑定任务）。
        route = _route_state(self._route_key)
        session: _AdsSession | None = None
        # 路由变更锁从摘除会话之前持有，直到物理 close 线程真实收敛：
        # 「摘除 → adsDelRoute」整段期间同路由新连接（含其他 Driver）都无法
        # 取得准入，active/closing 登记覆盖整个窗口（I1/I2）。
        try:
            route_timeout = max(self._config.timeout, 5.0) + 1.0
            try:
                await asyncio.wait_for(route.op_lock.acquire(), timeout=route_timeout)
            except TimeoutError:
                # 防御路径：同路由只允许一个活动 Driver，持锁者只能是本 Driver
                # 之前尚未收敛的物理关闭。绝不无锁下发 adsDelRoute。
                raise ProtocolError(
                    f"ADS close blocked for route {self._route_key!r}: route lock "
                    f"not acquired within {route_timeout:.3f}s; "
                    "physical close not attempted"
                ) from None
            try:
                # Driver 锁只在摘除注册表/会话的瞬间持有：注销命令经 Session
                # 命令门在锁外执行，避免 DEL_DEVICE_NOTIFICATION 的网络往返
                # 长时间挡住注册表操作。旧 Session 已摘除，新连接无法建立
                # （_lifecycle + 路由锁仍持有），因此注销不会跨 Session。
                async with self._lock:
                    session, subscriptions = self._detach_runtime()
                if session is not None:
                    # 登记先于底层 close 启动（I2）：close_local 与同路由建链
                    # 从此刻起即可观察到「物理关闭在途」，不存在先 close 后
                    # 登记的窗口。注意 active 仍保留本 Driver，直到物理收敛。
                    async with _ams_barrier_closing():
                        route.closing.add(session)
                for subscription in subscriptions:
                    subscription.deactivate()
                # Worker 取消不持任何 IO 锁：用户回调内可能再做驱动 IO（经
                # detach 后的快速失败返回）。
                for result in await asyncio.gather(
                    *(subscription.stop_worker() for subscription in subscriptions),
                    return_exceptions=True,
                ):
                    if isinstance(result, BaseException):
                        logger.exception("ADS subscription worker stop failed", exc_info=result)
                deregister_error: Exception | None = None
                for subscription in subscriptions:
                    try:
                        await subscription.deregister()
                    except Exception as exc:
                        # 一个 Handle 注销失败，不得跳过其他 Handle 的清理。
                        if deregister_error is None:
                            deregister_error = exc
                        logger.warning(
                            "%sADS subscription deregistration failed",
                            self._label,
                            exc_info=True,
                        )
                # 物理关闭与注销分别收敛，禁止将注销失败误当作已安全关端口。
                if session is not None:
                    await self._shutdown_session(session)
                if deregister_error is not None:
                    raise deregister_error
            finally:
                self._release_route_after_settle(route, session)
        finally:
            self._lifecycle.release()

    def _release_route_after_settle(self, route: _RouteState, session: _AdsSession | None) -> None:
        """物理关闭已收敛则立即释放路由锁；否则在收敛回调中释放。

        active/closing 摘除与锁释放在同一个 loop 回调内完成，状态对
        close_local 与等待建链者始终一致（Q5）。
        """
        if session is None:
            route.op_lock.release()
            return
        settled = session.shutdown_settled_future

        def _finish() -> None:
            confirmed = (
                settled is not None
                and settled.done()
                and not settled.cancelled()
                and settled.exception() is None
                and bool(settled.result())
            )
            if not confirmed:
                route.unsafe_reason = "physical ADS close not confirmed"
                logger.error(
                    "%sADS route %s quarantined: close unconfirmed",
                    self._label,
                    route.key,
                )
            route.active.discard(self)
            route.closing.discard(session)
            route.op_lock.release()

        if settled is None or settled.done():
            _finish()
        else:
            settled.add_done_callback(lambda _future: _finish())

    async def _shutdown_session(self, session: _AdsSession | None) -> None:
        """有界关闭会话：拒绝新命令、等待旧命令线程结束后关闭端口。"""
        if session is None:
            return
        session.route_key = self._route_key
        grace = max(self._config.timeout, 5.0) + 1.0
        # aclose 自身完成有界等待与放弃计数；放弃时端口保持打开并已记录，
        # 路由锁由收敛回调持有到物理线程真正结束。
        await session.aclose(grace)
        self._log_session_closed(session)

    async def _shutdown_session_inline(self, session: _AdsSession, route: _RouteState) -> None:
        """建链流程内当场关闭全新会话（调用方已持路由变更锁）。"""
        session.route_key = self._route_key
        grace = max(self._config.timeout, 5.0) + 1.0
        async with _ams_barrier_closing():
            route.closing.add(session)
        try:
            closed = await session.shutdown_inline(grace)
            if not closed:
                route.unsafe_reason = "inline physical ADS close not confirmed"
                raise ProtocolError(
                    f"ADS route {route.key!r} cannot retry: physical close unconfirmed"
                )
        except BaseException:
            # shutdown_inline 抛错或遭取消时，不能假设底层线程已安全退出。
            route.unsafe_reason = "inline physical ADS close interrupted"
            raise
        finally:
            route.active.discard(self)
            route.closing.discard(session)
        self._log_session_closed(session)

    def _log_session_closed(self, session: _AdsSession) -> None:
        stats = session.stats.snapshot()
        abnormal = (
            stats["timeouts"] or stats["shutdown_abandoned"] or stats["rejected_after_closing"]
        )
        # 每会话关闭时一行汇总：无异常计数用 INFO（每会话仅一行，低频、
        # 零基数），含超时/放弃/拒绝时升级 WARNING。
        level = logging.WARNING if abnormal else logging.INFO
        logger.log(level, "%sADS session closed stats=%s", self._label, stats)

    def is_open(self) -> bool:
        """本地 ADS session 是否处于已连接状态（含路由与符号解析完成）。"""
        return self._connected

    async def health(self) -> ConnectionHealth:
        """在当前业务连接上读取 ADS 状态；仅 PLC ADS State=RUN 为健康。

        会话快照在 Driver 状态锁内取得，实际 ``read_state`` 经 Session
        统一命令门在锁外执行，不被批量采集的状态锁阻塞；同一 Session 的
        命令串行性由 Session 两级闸门保证（见 :class:`_AdsSession`）。
        """
        async with self._lock:
            session = self._session
            if not self._connected or session is None:
                return ConnectionHealth(healthy=False, message="ADS session is not open")
        return await self._read_health(session)

    async def _read_health(self, session: _AdsSession) -> ConnectionHealth:
        """对指定 Session 执行一次 read_state；任何异常归一化为不健康。"""
        if not session.is_open:
            return ConnectionHealth(healthy=False, message="ADS session is not open")
        try:
            state = await session.execute("read_state")
            if state is None:
                return ConnectionHealth(healthy=False, message="ADS state read returned None")
            ads_state, device_state = state
            run_state = getattr(getattr(pyads, "constants", None), "ADSSTATE_RUN", 5)
            return ConnectionHealth(
                healthy=ads_state == run_state,
                message=(
                    f"ads_state={_ads_state_name(ads_state)}, " f"device_state={device_state}"
                ),
            )
        except Exception as exc:
            return ConnectionHealth(healthy=False, message=f"ADS state read failed: {exc}")

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Callable[[tuple[ProtocolSample, ...]], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> _TrackedADSSubscription:
        """复用当前连接注册已配置测点的周期 ADS 通知。

        注册过程包含多个 ADD_DEVICE_NOTIFICATION 往返：状态锁只在创建/
        登记订阅的瞬间持有，区块注册经 Session 命令门在锁外执行，不再
        长时间独占 Driver 状态锁。在途订阅始终登记在
        ``_starting_subscriptions`` 中：注册被取消时其后台清理仍会被
        ``close`` 等待收敛，晚到的通知 Handle 不会丢失。
        """
        if interval is None or interval <= 0:
            raise ConfigError("ADS subscription interval must be > 0")
        async with self._lock:
            session = self._session
            if not self._connected or session is None:
                raise ProtocolError("ADS subscribe requires an active connection")
            mapped = (
                tuple(self._mapped_point(point_id) for point_id in point_ids)
                if point_ids
                else tuple(self._points.values())
            )
            subscription = ADSSubscription(
                session,
                mapped,
                callback,
                cycle_time=interval,
                loop=asyncio.get_running_loop(),
                max_block_bytes=self._config.max_block_bytes,
                batch_size=self._config.push_batch_size,
                batch_wait=self._config.push_batch_wait,
                label=self._label,
            )
            # 先放入在途集合：注册协程若被取消，start 调度的后台清理仍由
            # driver.close 等待收敛，不会留下无人跟踪的订阅。
            self._starting_subscriptions.add(subscription)

        # 注意：CancelledError 不在此捕获——订阅保留在在途集合中，
        # start() 已自行调度后台注销任务，由后续 close/句柄收敛。
        try:
            await subscription.start()
        except ConfigError:
            # 配置级问题（重叠/超限）保持原错误类型，由上层按配置故障处理。
            await self._discard_starting(subscription)
            raise
        except Exception as exc:
            await self._discard_starting(subscription)
            # 传输级失败（超时/断链/设备拒绝）与读写路径同口径归一化；
            # subscription.start 已尝试注销已注册的区块句柄。
            raise ProtocolError(f"ADS subscribe failed: {exc}") from exc

        async with self._lock:
            if self._session is not session or not self._connected:
                # 注册途中会话被 close/reconnect 摘除：注销与 Worker 收敛
                # 由 close 路径（同一注销任务）与这里的 close 共同保证。
                detached = True
            else:
                detached = False
                self._starting_subscriptions.discard(subscription)
                self._subscriptions.add(subscription)
        if detached:
            await subscription.close()
            raise ProtocolError("ADS session closed while subscribing")
        return _TrackedADSSubscription(self, subscription)

    async def _discard_starting(self, subscription: ADSSubscription) -> None:
        """从在途注册表移除已确定失败/完成的订阅（状态锁内）。"""
        async with self._lock:
            self._starting_subscriptions.discard(subscription)

    async def read_one(self, point: str) -> ProtocolSample:
        """读取一个逻辑点；低频路径，不走 Sum Read、分段或读取分组缓存。

        只接受已配置 Point Key，地址在连接建立时预先解析。

        会话与点地址在 Driver 状态锁内快照，实际 ADS 读经 Session 统一
        命令门在锁外执行；本地会话引用保证命令返回前 Session 不会被销毁。
        """
        async with self._lock:
            session = self._require_session_locked("read")
            mapped = self._resolve_point(point)
        point_id = mapped.point_id
        try:
            value = await session.execute(
                "read",
                mapped.group,
                mapped.offset,
                _plc_datatype(mapped.data_type),
            )
        except Exception as exc:
            if _is_point_level_error(exc):
                return _bad_sample(point_id)
            raise ProtocolError(f"ADS read failed: {exc}") from exc
        try:
            scalar = _as_point_scalar(value)
        except TypeError:
            return _bad_sample(point_id)
        return ProtocolSample(
            point_id=point_id,
            value=scalar,
            timestamp=(received_at := datetime.now(UTC)),
            receive_time=received_at,
            quality=Quality.GOOD,
        )

    def _require_session_locked(self, action: str) -> _AdsSession:
        """返回当前已连接会话；调用方必须持有 Driver 状态锁。"""
        session = self._session
        if not self._connected or session is None:
            raise ProtocolError(f"ADS {action} requires an active connection")
        return session

    async def write_one(
        self,
        write: ProtocolWrite,
    ) -> ProtocolWriteResult:
        """写入一个逻辑点；低频路径，不使用 Sum Write 批量规划。

        ProtocolWrite.point 必须是已配置的 Point Key。

        配置/取值级问题返回失败结果；连接/传输级问题统一抛 ProtocolError。

        不存在自动重发：取消发生在命令进入 AdsLib 之后时，工作线程仍会
        跑完唯一一次写入，结果被丢弃（见 :class:`_AdsSession`）。
        """
        async with self._lock:
            session = self._require_session_locked("write")
            mapped = self._resolve_point(write.point)
        try:
            return await self._write_single_point(session, mapped, write.value)
        except Exception as exc:
            raise ProtocolError(f"ADS write failed: {exc}") from exc

    async def read_many(
        self,
        points: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        """使用 ADS Sum Read 批量读取，返回按输入顺序排列的样本元组。

        入参只接受已配置 Point Key；传输失败不降级为逐点读取。
        """
        raw = await self._read_values(points)
        # 全批次共享同一读取时刻——Driver 在获得读取结果时生成 UTC 时间戳。
        received_at = datetime.now(UTC)
        return tuple(
            ProtocolSample(point_id=pid, value=value, timestamp=received_at, quality=quality, receive_time=received_at)
            for pid, (value, quality) in zip(points, raw, strict=True)
        )

    async def _read_values(
        self,
        points: Sequence[str],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """按已配置的地址直接执行 Sum Read。

        会话快照、点解析与请求编译在 Driver 状态锁内完成；每个 Sum Read
        分块是独立的 Session 命令单元，在锁外经命令门执行——分块之间允许
        health/控制写插入。整批只使用快照 Session，关闭/重连会使在途批次
        失败，绝不混合新旧 Session 的结果。
        """
        if not points:
            return ()
        async with self._lock:
            session = self._require_session_locked("read")
            try:
                mapped = tuple(self._resolve_point(point) for point in points)
                cached = self._read_cache.get(mapped)
            except ProtocolError:
                raise
            except Exception as exc:
                raise ProtocolError(f"ADS read failed: {exc}") from exc
        if cached is None:
            # 缓存未命中：ctypes 请求数组构造与逐点解码器编译是纯 CPU
            # 操作且只依赖不可变 ADSPoint 元组，放在锁外，不挡其他路径
            # 取状态快照。并发未命中最多重复编译一次，结果等价。
            try:
                compiled = (
                    self._build_sum_requests(mapped),
                    self._build_sum_decoders(mapped),
                )
            except ProtocolError:
                raise
            except Exception as exc:
                raise ProtocolError(f"ADS read failed: {exc}") from exc
            async with self._lock:
                self._store_sum_read_cache_locked(mapped, compiled)
            cached = compiled
        requests, decoders = cached
        try:
            return await self._execute_sum_read(session, mapped, requests, decoders)
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError(f"ADS read failed: {exc}") from exc

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """按已解析 index 地址批量写入。

        配置级问题收敛为单点失败；连接/传输级问题统一抛 ProtocolError。

        写入顺序语义：

        - 同一批次内各分块严格按输入顺序执行（同一 Task 串行 await）；
        - 每个分块是独立 Session 命令单元，分块之间允许其他命令插入，
          因此本批次不是跨其他并发写入的原子事务——不同 API 调用之间的
          先后顺序本来就由调度决定，本方法只保证每条写入在 Session 上
          严格串行且不自动重发；
        - 已确认成功/失败按设备返回码逐点分类；分块传输失败时，尚未确认
          的分块不重发（请求可能已到达设备），由上层按 UNKNOWN 语义处理。
        """
        if not writes:
            return ()

        # 锁内只取语义稳定的快照：Session 引用 + 不可变 ADSPoint 元组
        # （frozen dataclass）。点表热更新只替换 self._points 映射，
        # 已取得的本批次映射不会改变；coercion/编码全部在锁外完成。
        async with self._lock:
            session = self._require_session_locked("write")
            mapped_batch = tuple(self._resolve_point(write.point) for write in writes)
            chunk_size = self._config.max_subs_per_sum

        results: list[ProtocolWriteResult | None] = [None] * len(writes)
        prepared: list[tuple[int, ADSPoint, bytes]] = []
        for index, (write, point) in enumerate(zip(writes, mapped_batch, strict=True)):
            if not point.address_resolved:
                results[index] = ProtocolWriteResult(
                    point_id=write.point,
                    outcome=WriteOutcome.CONFIRMED_FAILURE,
                    message="ADS address is unresolved",
                )
                continue
            try:
                raw_value = _coerce_write_value(write.value, point.data_type)
                payload = _encode_value(raw_value, point)
            except (ConfigError, TypeError, ValueError, OverflowError) as exc:
                results[index] = ProtocolWriteResult(
                    point_id=write.point,
                    outcome=WriteOutcome.CONFIRMED_FAILURE,
                    message=str(exc),
                )
                continue
            prepared.append((index, point, payload))

        try:
            for start in range(0, len(prepared), chunk_size):
                chunk = prepared[start : start + chunk_size]
                request = self._encode_sum_write(chunk)
                raw = await session.execute(
                    "read_write",
                    ADSIGRP_SUMUP_WRITE,
                    len(chunk),
                    None,
                    request,
                    None,
                    check_length=False,
                )
                errors = _decode_sum_write_errors(raw, len(chunk))
                for (index, point, _), error in zip(chunk, errors, strict=True):
                    results[index] = ProtocolWriteResult(
                        point_id=point.point_id,
                        # error == 0 为设备确认成功；否则设备返回明确
                        # ADS 错误码——两种结果都有协议证据。
                        outcome=(
                            WriteOutcome.CONFIRMED_SUCCESS
                            if error == 0
                            else WriteOutcome.CONFIRMED_FAILURE
                        ),
                        message=None if error == 0 else f"ADS write error {error}",
                    )
        except Exception as exc:
            raise ProtocolError(f"ADS sum write failed: {exc}") from exc

        if any(result is None for result in results):
            raise RuntimeError("ADS sum write did not produce a result for every point")
        return tuple(result for result in results if result is not None)

    @staticmethod
    def _encode_sum_write(chunk: Sequence[tuple[int, ADSPoint, bytes]]) -> bytearray:
        """构造 Sum Write 请求载荷（逐点头部拼接 + 逐点数据拼接）。"""
        headers = b"".join(
            struct.pack("<III", point.group, point.offset, len(payload))
            for _, point, payload in chunk
        )
        return bytearray(headers + b"".join(payload for _, _, payload in chunk))

    async def _write_single_point(
        self,
        session: _AdsSession,
        mapped: ADSPoint,
        value: object,
    ) -> ProtocolWriteResult:
        """按已解析 index 地址写入单点；配置/取值级问题收敛为单点失败。

        连接/传输级异常原样抛出，由调用方上报，不擅自关闭会话或重发。
        """
        if not mapped.address_resolved:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message="ADS address is unresolved",
            )

        try:
            datatype = _plc_datatype(mapped.data_type)
        except ConfigError as exc:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message=str(exc),
            )

        try:
            raw_value = _coerce_write_value(
                value,
                mapped.data_type,
            )
        except (TypeError, ValueError) as exc:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_FAILURE,
                message=str(exc),
            )

        try:
            await session.execute(
                "write",
                mapped.group,
                mapped.offset,
                raw_value,
                datatype,
            )
        except Exception as exc:
            # 点级 ADS 错误（如 symbol 在 PLC 侧已删除）只收敛为单点失败；
            # 连接/传输级异常继续抛出，由调用方统一断线处理。
            if _is_point_level_error(exc):
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.CONFIRMED_FAILURE,
                    message=str(exc) or type(exc).__name__,
                )
            raise
        return ProtocolWriteResult(
            point_id=mapped.point_id,
            outcome=WriteOutcome.CONFIRMED_SUCCESS,
        )

    async def interrogate(
        self, common_address: int | None = None, *, qoi: int = 20
    ) -> tuple[ProtocolSample, ...]:
        """ADS 不支持 IEC104 式总召能力。"""
        del common_address, qoi
        raise ProtocolCapabilityError("ads does not support interrogation")

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: Callable[[tuple[ProtocolSample, ...]], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        """ADS 的远程数据获取只有原生订阅（subscribe），没有被动监听语义。"""
        del point_ids, callback, interval
        raise ProtocolCapabilityError("ads does not support passive listen — use subscribe")

    def _build_sum_requests(self, points: tuple[ADSPoint, ...]) -> tuple[Any, ...]:
        """编译可直接交给 pyads.read_write 的分批 Sum Read 请求。"""

        class _SumReadItem(Structure):
            _fields_ = [
                ("iGroup", c_uint32),
                ("iOffset", c_uint32),
                ("size", c_uint32),
            ]

        limit = self._config.max_subs_per_sum
        return tuple(
            (_SumReadItem * len(chunk))(
                *(_SumReadItem(point.group, point.offset, point.size) for point in chunk)
            )
            for start in range(0, len(points), limit)
            if (chunk := points[start : start + limit])
        )

    def _build_sum_decoders(
        self, points: tuple[ADSPoint, ...]
    ) -> tuple[Callable[[bytes | memoryview, int], PointScalar], ...]:
        """编译逐点解码器，与请求地址保持相同顺序。"""
        return tuple(compile_decoder(point) for point in points)

    def _store_sum_read_cache_locked(
        self,
        points: tuple[ADSPoint, ...],
        compiled: tuple[
            tuple[Any, ...],
            tuple[Callable[[bytes | memoryview, int], PointScalar], ...],
        ],
    ) -> None:
        """把锁外编译结果写回 FIFO（容量 32）缓存；调用方持状态锁。

        键是不可变 ADSPoint 元组：点表热更新产生的是新对象元组，旧键
        自然失效，无需代际序号。并发未命中的重复写回内容等价。
        """
        if points in self._read_cache:
            return
        if len(self._read_cache) >= 32:
            self._read_cache.pop(next(iter(self._read_cache)))
        self._read_cache[points] = compiled

    async def _execute_sum_read(
        self,
        session: _AdsSession,
        points: Sequence[ADSPoint],
        requests: tuple[Any, ...],
        decoders: tuple[Callable[[bytes | memoryview, int], PointScalar], ...],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """逐分块执行 Sum Read（分块间释放 Session 命令门）后统一按序解码。"""
        responses: list[tuple[Any, bytes]] = []
        for chunk in requests:
            response = await session.execute(
                "read_write",
                ADSIGRP_SUMUP_READ,
                len(chunk),
                None,
                chunk,
                None,
                check_length=False,
            )
            if response is None:
                raise ProtocolError("ADS Sum Read received no response")
            raw = response if isinstance(response, bytes) else bytes(response)
            expected = 4 * len(chunk) + sum(item.size for item in chunk)
            if len(raw) < expected:
                raise ProtocolError(
                    f"ADS sum read returned {len(raw)} bytes; expected at least {expected}"
                )
            responses.append((chunk, raw))

        results: list[tuple[PointScalar, Quality]] = []
        decoder_index = 0
        for chunk, raw in responses:
            offset = 4 * len(chunk)
            errors = struct.unpack_from(f"<{len(chunk)}I", raw)
            data_view = memoryview(raw)
            for item, error in zip(chunk, errors, strict=True):
                size = item.size
                offset += size
                decoder = decoders[decoder_index]
                decoder_index += 1
                if error:
                    results.append((None, Quality.BAD))
                    continue
                try:
                    results.append((decoder(data_view, offset - size), Quality.GOOD))
                except (TypeError, ValueError, UnicodeDecodeError, struct.error):
                    results.append((None, Quality.BAD))
        return tuple(results)

    async def resolve_symbol(self, symbol: str) -> tuple[int, int]:
        """按需查询 PLC 符号地址；同一 ADS session 内复用解析结果。"""
        async with self._lock:
            session = self._require_session_locked("resolve_symbol")
            cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached[0], cached[1]
        info = await session.execute("get_symbol", symbol)
        group, offset = symbol_index_address(symbol, info)
        raw_size = getattr(info, "size", None)
        size = raw_size if type(raw_size) is int and raw_size > 0 else None
        resolved = (group, offset, size)
        async with self._lock:
            # 并发解析同符号时不覆盖；会话已切换则结果属于旧会话，不入缓存。
            if self._session is session and self._connected:
                self._symbol_cache.setdefault(symbol, resolved)
        return group, offset

    async def _resolve_symbol_locked(self, symbol: str) -> tuple[int, int, int | None]:
        """connect 建链流程内使用：调用方持 Driver 状态锁且会话不会切换。"""
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached
        session = self._session
        if session is None:
            raise ProtocolError("ADS connection is not available")
        info = await session.execute("get_symbol", symbol)
        group, offset = symbol_index_address(symbol, info)
        raw_size = getattr(info, "size", None)
        size = raw_size if type(raw_size) is int and raw_size > 0 else None
        resolved = (group, offset, size)
        self._symbol_cache[symbol] = resolved
        return resolved

    async def _resolve_configured_points(self) -> None:
        """连接建立后一次性解析当前点表所有符号，读写不再执行符号查询。"""
        resolved = dict(self._points)
        for point_id, mapped in resolved.items():
            if mapped.address_resolved:
                continue
            if mapped.symbol is None:
                raise ConfigError(f"ADS point '{point_id}' has no address")
            group, offset, plc_size = await self._resolve_symbol_locked(mapped.symbol)
            if mapped.data_type != "STRING" and plc_size is not None and plc_size != mapped.size:
                raise ConfigError(
                    f"ADS point '{point_id}': PLC size {plc_size} "
                    f"does not match {mapped.data_type} width {mapped.size}"
                )
            resolved[point_id] = mapped.resolved(index_group=group, index_offset=offset)
        self._points = resolved
        self._read_cache.clear()

    def _invalidate_symbol_addresses(self) -> None:
        self._read_cache.clear()
        self._symbol_cache.clear()
        self._points = {point_id: point.unresolved() for point_id, point in self._points.items()}

    def _mapped_point(self, point_id: str) -> ADSPoint:
        mapped = self._points.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection " f"'{self._point_table_id}'"
            )
        return mapped

    def _resolve_point(self, point: str) -> ADSPoint:
        """仅允许按当前 PointTable 已配置的 Key 寻址。"""
        if not isinstance(point, str):
            raise ConfigError("protocol point must be a configured point key")
        return self._mapped_point(point)

    def _detach_runtime(
        self,
    ) -> tuple[_AdsSession | None, tuple[ADSSubscription, ...]]:
        """在 Driver lock 内原子摘除当前会话与订阅。"""
        session, self._session = self._session, None
        subscriptions = tuple(self._subscriptinstance(value, int | float):
            raise TypeError(f"ADS {ads_type} requires numeric value")
        return float(value)

    raise ValueError(f"unsupported ADS type '{ads_type}'")


class _TrackedADSSubscription:
    """从 Driver 注册表中自动移除的订阅句柄。"""

    def __init__(
        self,
        owner: ADSDriver,
        subscription: ADSSubscription,
    ) -> None:
        self._owner = owner
        self._subscription = subscription
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        owner = self._owner
        subscription = self._subscription
        # 一个物理 Handle 注销失败也必须停止本逻辑订阅的交付 Worker。
        # close() 复用 Subscription 已有的幂等、可取消等待收敛管线。
        try:
            await subscription.close()
        finally:
            async with owner._lock:
                owner._subscriptions.discard(subscription)
                owner._starting_subscriptions.discard(subscription)
