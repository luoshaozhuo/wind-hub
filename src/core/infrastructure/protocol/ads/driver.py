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
import weakref
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
    _as_point_scalar,
    _bad_sample,
    _coerce_write_value,
    _decode_sum_write_errors,
    _encode_value,
    _is_point_level_error,
    _plc_datatype,
    compile_decoder,
    encode_sum_write,
)
from .config import ADSConfig, parse_ads_config
from .mapping import ADSPoint, parse_ads_point, symbol_index_address
from .session import ADSCommandNotSentError, _AdsSession
from .subscription import ADSSubscription


class _SumReadItem(Structure):
    """pyads Sum Read 请求头部，类型只创建一次。"""

    _fields_ = [
        ("iGroup", c_uint32),
        ("iOffset", c_uint32),
        ("size", c_uint32),
    ]


logger = logging.getLogger(__name__)
_REMOTE_ROUTE_LOCK = threading.Lock()
_LOCAL_AMS_LOCK = threading.Lock()
_LOCAL_LIFECYCLE = asyncio.Lock()
_ACTIVE_DRIVERS: weakref.WeakSet[ADSDriver] = weakref.WeakSet()
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
        self._unsafe_close = False
        self._pending_close: asyncio.Future[bool] | None = None
        _ACTIVE_DRIVERS.add(self)

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
        """Initialize process-local AMS identity outside the event loop."""
        async with _LOCAL_LIFECYCLE:
            await asyncio.to_thread(cls._initialize_local_sync, config)

    @classmethod
    async def close_local(cls) -> None:
        """Close local AMS only after all driver sessions have safely stopped."""
        async with _LOCAL_LIFECYCLE:
            if any(
                driver._session is not None
                or (driver._pending_close is not None and not driver._pending_close.done())
                for driver in _ACTIVE_DRIVERS
            ):
                raise ProtocolError("ADS local port cannot close while sessions are active")
            await asyncio.to_thread(cls._close_local_sync)

    @staticmethod
    def _close_local_sync() -> None:
        global _LOCAL_AMS, _LOCAL_PORT_OPEN
        with _LOCAL_AMS_LOCK:
            if _LOCAL_PORT_OPEN:
                pyads.close_port()
            _LOCAL_PORT_OPEN = False
            _LOCAL_AMS = None

    def _new_connection(self) -> Any:
        """创建配置一致、尚未 open 的 pyads Connection。"""
        connection = pyads.Connection(
            self._config.target_net_id or None,
            self._config.target_port,
            self._config.host,
        )
        return connection

    async def connect(self) -> None:
        """Connect only after the preceding physical close has settled."""
        async with self._lifecycle:
            pending = self._pending_close
            if self._unsafe_close or (pending is not None and not pending.done()):
                raise ProtocolError("ADS previous physical close is not confirmed")
            if pending is not None and (pending.cancelled() or pending.exception() or not pending.result()):
                self._unsafe_close = True
                raise ProtocolError("ADS previous physical close failed")
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        async with self._lock:
            if self._connected and self._session is not None:
                return
            if self._session is not None:
                stale = self._session
                self._session = None
                await self._shutdown_session_inline(stale)
            last_health: ConnectionHealth | None = None
            for attempt in range(2 if self._config.route_auto_add else 1):
                session = _AdsSession(
                    self._new_connection(),
                    self._label,
                    max_notifications=self._config.max_notifications_per_connection,
                )
                connection = session.connection
                self._session = session
                try:
                    await asyncio.to_thread(connection.open)
                    if not connection.is_open:
                        raise ConnectionError("ADS session did not open")
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
                    self._session = None
                    await self._shutdown_session_inline(session)
                    raise
                except Exception:
                    last_health = ConnectionHealth(
                        healthy=False,
                        message=f"ADS connection failed for {self._config.host}",
                    )
                finally:
                    if not self._connected and self._session is session:
                        self._session = None
                        await self._shutdown_session_inline(session)

                if attempt == 0 and self._config.route_auto_add:
                    identity = _LOCAL_AMS
                    if identity is None:
                        raise ConfigError("ADS local AMS identity is not initialized")
                    try:
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
        """Detach the current session and drain its work before releasing lifecycle."""
        try:
            async with self._lock:
                session, subscriptions = self._detach_runtime()
            for subscription in subscriptions:
                subscription.deactivate()
            worker_error: BaseException | None = None
            for result in await asyncio.gather(
                *(subscription.stop_worker() for subscription in subscriptions),
                return_exceptions=True,
            ):
                if isinstance(result, BaseException):
                    if worker_error is None:
                        worker_error = result
                    logger.error(
                        "%sADS subscription worker stop failed: %r",
                        self._label, result, exc_info=result,
                    )
            deregister_error: Exception | None = None
            for subscription in subscriptions:
                try:
                    await subscription.deregister()
                except Exception as exc:
                    if deregister_error is None:
                        deregister_error = exc
                    logger.warning(
                        "%sADS subscription deregistration failed",
                        self._label,
                        exc_info=True,
                    )
            if session is not None:
                await self._shutdown_session(session)
            if deregister_error is not None and session is None:
                raise deregister_error
            if worker_error is not None:
                raise ProtocolError(
                    "ADS subscription worker cancellation not confirmed"
                ) from worker_error
        finally:
            self._lifecycle.release()

    async def _shutdown_session(self, session: _AdsSession) -> None:
        """有界关闭会话：拒绝新命令、等待旧命令线程结束后关闭端口。"""
        session.route_key = self._route_key
        grace = max(self._config.timeout, 5.0) + 1.0
        closed = await session.aclose(grace)
        self._pending_close = session.shutdown_settled_future
        if not closed:
            self._unsafe_close = True
        self._log_session_closed(session)
        if not closed:
            raise ProtocolError(
                f"ADS physical close not confirmed for route {self._route_key!r}"
            )

    async def _shutdown_session_inline(self, session: _AdsSession) -> None:
        """Close an unpublished session before attempting a new connection."""
        grace = max(self._config.timeout, 5.0) + 1.0
        try:
            closed = await asyncio.shield(session.shutdown_inline(grace))
        except BaseException:
            self._unsafe_close = True
            raise
        if not closed:
            self._unsafe_close = True
            raise ProtocolError("ADS inline physical close not confirmed")
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
                tuple(self._resolve_point(point_id) for point_id in point_ids)
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
                max_delay=self._config.max_delay,
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

        配置/取值级问题返回失败；提交后确认不明返回 UNKNOWN，不自动重发。

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
            ProtocolSample(
                point_id=pid,
                value=value,
                timestamp=received_at,
                quality=quality,
                receive_time=received_at,
            )
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
                    tuple(compile_decoder(point) for point in mapped),
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

        for start in range(0, len(prepared), chunk_size):
            chunk = prepared[start : start + chunk_size]
            request = encode_sum_write(chunk)
            try:
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
            except Exception as exc:
                # 被命令门明确拒绝属于未发送；其他异常可能已提交。
                not_sent = isinstance(exc, ADSCommandNotSentError)
                for index, point, _ in chunk:
                    results[index] = ProtocolWriteResult(
                        point_id=point.point_id,
                        outcome=(
                            WriteOutcome.CONFIRMED_FAILURE
                            if not_sent
                            else WriteOutcome.UNKNOWN
                        ),
                        message=(
                            f"ADS sum write not sent: {exc}"
                            if not_sent
                            else f"ADS sum write confirmation unavailable: {exc}"
                        ),
                    )
                for index, point, _ in prepared[start + len(chunk) :]:
                    results[index] = ProtocolWriteResult(
                        point_id=point.point_id,
                        outcome=WriteOutcome.CONFIRMED_FAILURE,
                        message="ADS sum write not sent after previous chunk failed",
                    )
                break
            for (index, point, _), error in zip(chunk, errors, strict=True):
                results[index] = ProtocolWriteResult(
                    point_id=point.point_id,
                    outcome=(
                        WriteOutcome.CONFIRMED_SUCCESS
                        if error == 0
                        else WriteOutcome.CONFIRMED_FAILURE
                    ),
                    message=None if error == 0 else f"ADS write error {error}",
                )

        if any(result is None for result in results):
            raise RuntimeError("ADS sum write did not produce a result for every point")
        return tuple(result for result in results if result is not None)

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
            if isinstance(exc, ADSCommandNotSentError):
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.CONFIRMED_FAILURE,
                    message=str(exc),
                )
            # 确定的点级拒绝是失败；传输确认丢失时可能已执行，必须 UNKNOWN。
            if _is_point_level_error(exc):
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.CONFIRMED_FAILURE,
                    message=str(exc) or type(exc).__name__,
                )
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.UNKNOWN,
                message=f"ADS write confirmation unavailable: {exc}",
            )
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

        limit = self._config.max_subs_per_sum
        return tuple(
            (_SumReadItem * len(chunk))(
                *(_SumReadItem(point.group, point.offset, point.size) for point in chunk)
            )
            for start in range(0, len(points), limit)
            if (chunk := points[start : start + limit])
        )

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

    def _resolve_point(self, point_id: str) -> ADSPoint:
        """仅允许按当前 PointTable 已配置的 Key 寻址。"""
        if not isinstance(point_id, str):
            raise ConfigError("protocol point must be a configured point key")
        mapped = self._points.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection " f"'{self._point_table_id}'"
            )
        return mapped

    def _detach_runtime(
        self,
    ) -> tuple[_AdsSession | None, tuple[ADSSubscription, ...]]:
        """在 Driver lock 内原子摘除当前会话与订阅。"""
        session, self._session = self._session, None
        subscriptions = tuple(self._subscriptions | self._starting_subscriptions)
        self._subscriptions.clear()
        self._starting_subscriptions.clear()
        self._connected = False
        return session, subscriptions


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
