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
from typing import Any, cast

import pyads  # type: ignore[import-untyped]
from pyads.constants import ADSIGRP_SUMUP_READ, ADSIGRP_SUMUP_WRITE  # type: ignore[import-untyped]

from core.application.errors import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.port.protocol import ListenHandle, SubscriptionHandle
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

from .codec import compile_decoder
from .config import ADSConfig, parse_ads_config
from .mapping import ADSPoint, parse_ads_point, symbol_index_address
from .subscription import ADSSubscription

_ADSERR_SYMBOL_NOT_FOUND = 1808

logger = logging.getLogger(__name__)
_REMOTE_ROUTE_LOCK = threading.Lock()
_LOCAL_AMS_LOCK = threading.RLock()
_LOCAL_AMS: ADSLocalConfig | None = None
_LOCAL_PORT_OPEN = False


class _AdsSession:
    """一条 pyads 连接的线程级命令闸门。

    pyads 连接是同步请求/响应通道，不支持多线程并发收发。所有命令在
    ``run`` 中经同一把 ``threading.Lock`` 串行（比 asyncio 锁更强：
    即使等待协程被取消、异步锁被释放，仍在运行的旧命令线程仍然持锁）。

    ``shutdown`` 必须获取同一把锁后才关闭端口，因此：

    - 关闭期间不可能有旧命令线程仍在使用该连接（无 use-after-close）；
    - 外层 ``asyncio`` 超时/取消后迟到返回的旧线程不可能关闭新会话；
    - ``shutdown`` 的等待上界 = 在途命令的 pyads 原生超时 + grace。

    获取锁超过 grace 仍失败时放弃关闭该端口（显式记录），绝不无限等待。
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self._cmd_lock = threading.Lock()
        self._closing = False

    @property
    def connection(self) -> Any:
        return self._connection

    @property
    def is_open(self) -> bool:
        return bool(self._connection.is_open)

    def notification(self, datatype: object) -> Callable[..., Callable[..., None]]:
        return cast(
            Callable[..., Callable[..., None]],
            self._connection.notification(datatype),
        )

    def run(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """在命令锁内执行一条 pyads 同步命令；关闭开始后快速拒绝。"""
        with self._cmd_lock:
            if self._closing:
                raise ProtocolError(f"ADS session is closing; command '{name}' rejected")
            return getattr(self._connection, name)(*args, **kwargs)

    def shutdown(self, grace: float) -> bool:
        """等待在途命令结束后关闭连接；超时放弃，返回 False。"""
        if not self._cmd_lock.acquire(timeout=grace):
            logger.error(
                "ADS session shutdown gave up after %.3fs: a command thread is "
                "still running; port left open (no close issued)",
                grace,
            )
            return False
        try:
            self._closing = True
            try:
                self._connection.close()
            except Exception:
                logger.exception("ADS connection close failed")
        finally:
            self._cmd_lock.release()
        return True


def _ads_state_name(value: object) -> str:
    """把 pyads ADSSTATE_* 数值转成可诊断名称；未知值保留原值。"""
    constants = getattr(pyads, "constants", None)
    if constants is not None:
        for name in dir(constants):
            if name.startswith("ADSSTATE_") and getattr(constants, name, object()) == value:
                return name.removeprefix("ADSSTATE_")
    return str(value)


def _plc_datatype(ads_name: str) -> Any:
    datatype = getattr(pyads, f"PLCTYPE_{ads_name}", None)
    if datatype is None:
        raise ConfigError(f"pyads does not expose PLCTYPE_{ads_name}")
    return datatype


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
        await asyncio.to_thread(cls._initialize_local_sync, config)

    @classmethod
    async def close_local(cls) -> None:
        """释放进程级本地 AMS 端口；不得在其他 ADS Driver 工作期间调用。"""
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
        """连接并检查 PLC RUN 状态；失败时可添加一次反向路由再重连。

        与 :meth:`close` 经 ``_lifecycle`` 互斥：旧会话（路由、端口、
        命令线程）完全结束前不会建立新会话。
        """
        async with self._lifecycle:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        async with self._lock:
            if self._connected and self._session is not None:
                return
            await self._shutdown_session(self._session)
            self._session = None
            last_health: ConnectionHealth | None = None

            for attempt in range(2 if self._config.route_auto_add else 1):
                session = _AdsSession(self._new_connection())
                connection = session.connection
                self._session = session
                try:
                    await asyncio.to_thread(connection.open)
                    if not connection.is_open:
                        raise ConnectionError("ADS session did not open")
                    await asyncio.to_thread(
                        connection.set_timeout, int(self._config.timeout * 1000)
                    )
                    last_health = await self._health_locked()
                    if last_health.healthy:
                        self._invalidate_symbol_addresses()
                        await self._resolve_configured_points()
                        self._connected = True
                        return
                except Exception:
                    last_health = ConnectionHealth(
                        healthy=False,
                        message=f"ADS connection failed for {self._config.host}",
                    )
                finally:
                    if not self._connected:
                        self._session = None
                        await self._shutdown_session(session)

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
        try:
            async with self._lock:
                session, subscriptions = self._detach_runtime()
            for subscription in subscriptions:
                subscription.deactivate()
            # Worker 取消放在 IO 锁外：用户回调内可能再做驱动 IO（经
            # detach 后的快速失败返回），持锁取消会自锁。
            for result in await asyncio.gather(
                *(subscription.stop_worker() for subscription in subscriptions),
                return_exceptions=True,
            ):
                if isinstance(result, BaseException):
                    logger.exception("ADS subscription worker stop failed", exc_info=result)
            async with self._lock:
                for subscription in subscriptions:
                    # 幂等：与 tracked handle.close 并发时收敛到同一任务。
                    await subscription.deregister()
                if session is not None:
                    await self._shutdown_session(session)
        finally:
            self._lifecycle.release()

    async def _shutdown_session(self, session: _AdsSession | None) -> None:
        """有界关闭会话：等待旧命令线程结束后再关闭端口，超时则放弃并记录。"""
        if session is None:
            return
        grace = max(self._config.timeout, 5.0) + 1.0
        try:
            await asyncio.wait_for(asyncio.to_thread(session.shutdown, grace), timeout=grace + 2.0)
        except TimeoutError:
            logger.error("ADS session shutdown exceeded %.3fs and was abandoned", grace + 2.0)

    def is_open(self) -> bool:
        """本地 ADS session 是否处于已连接状态（含路由与符号解析完成）。"""
        return self._connected

    async def health(self) -> ConnectionHealth:
        """在当前业务连接上读取 ADS 状态；仅 PLC ADS State=RUN 为健康。"""
        # pyads 单连接不是线程安全的请求/响应通道：read_state 必须与
        # 读写、通知注册等所有命令流量经同一把锁串行，否则并发命令的
        # AMS 帧会串扰，产生 ADSError(-1) 假失败与假不健康。
        async with self._lock:
            return await self._health_locked()

    async def _health_locked(self) -> ConnectionHealth:
        """调用方必须已持有 ``_lock``（connect 建链流程内复用）。"""
        session = self._session
        if session is None or not session.is_open:
            return ConnectionHealth(healthy=False, message="ADS session is not open")
        try:
            state = await asyncio.to_thread(session.run, "read_state")
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
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> _TrackedADSSubscription:
        """复用当前连接注册已配置测点的周期 ADS 通知。"""
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
            )
            # 先放入在途集合：注册协程若被取消，start 调度的后台清理仍由
            # driver.close 等待收敛，不会留下无人跟踪的订阅。
            self._starting_subscriptions.add(subscription)
            try:
                await subscription.start()
            except ConfigError:
                # 配置级问题（重叠/超限）保持原错误类型，由上层按配置故障处理。
                self._starting_subscriptions.discard(subscription)
                raise
            except Exception as exc:
                self._starting_subscriptions.discard(subscription)
                # 传输级失败（超时/断链/设备拒绝）与读写路径同口径归一化；
                # subscription.start 已尝试注销已注册的区块句柄。
                raise ProtocolError(f"ADS subscribe failed: {exc}") from exc
            self._starting_subscriptions.discard(subscription)
            self._subscriptions.add(subscription)
        return _TrackedADSSubscription(self, subscription)

    async def read_one(self, point: str) -> ProtocolSample:
        """读取一个逻辑点；低频路径，不走 Sum Read、分段或读取分组缓存。

        只接受已配置 Point Key，地址在连接建立时预先解析。
        """
        async with self._lock:
            session = self._session
            if not self._connected or session is None:
                raise ProtocolError("ADS read requires an active connection")
            mapped = self._resolve_point(point)
            point_id = mapped.point_id
            try:
                value = await asyncio.to_thread(
                    session.run,
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
                timestamp=datetime.now(UTC),
                quality=Quality.GOOD,
            )

    async def write_one(
        self,
        write: ProtocolWrite,
    ) -> ProtocolWriteResult:
        """写入一个逻辑点；低频路径，不使用 Sum Write 批量规划。

        ProtocolWrite.point 必须是已配置的 Point Key。

        配置/取值级问题返回失败结果；连接/传输级问题统一抛 ProtocolError。
        """
        mapped = self._resolve_point(write.point)
        write_value = write.value
        async with self._lock:
            if not self._connected or self._session is None:
                raise ProtocolError("ADS write requires an active connection")
            try:
                return await self._write_single_point(mapped, write_value)
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
            ProtocolSample(point_id=pid, value=value, timestamp=received_at, quality=quality)
            for pid, (value, quality) in zip(points, raw, strict=True)
        )

    async def _read_values(
        self,
        points: Sequence[str],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """按已配置的地址直接执行 Sum Read。"""
        if not points:
            return ()
        async with self._lock:
            session = self._session
            if not self._connected or session is None:
                raise ProtocolError("ADS read requires an active connection")
            try:
                mapped = tuple(self._resolve_point(point) for point in points)
                return await self._read_sum_raw(session, mapped)
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
        """
        if not writes:
            return ()

        async with self._lock:
            session = self._session
            if not self._connected or session is None:
                raise ProtocolError("ADS write requires an active connection")

            results: list[ProtocolWriteResult | None] = [None] * len(writes)
            mapped_batch = [self._resolve_point(write.point) for write in writes]
            prepared: list[tuple[int, ADSPoint, bytes]] = []
            for index, (write, point) in enumerate(zip(writes, mapped_batch, strict=True)):
                if not point.address_resolved:
                    results[index] = ProtocolWriteResult(
                        point_id=write.point_id,
                        outcome=WriteOutcome.CONFIRMED_FAILURE,
                        message="ADS address is unresolved",
                    )
                    continue
                try:
                    raw_value = _coerce_write_value(write.value, point.data_type)
                    payload = _encode_value(raw_value, point)
                except (ConfigError, TypeError, ValueError, OverflowError) as exc:
                    results[index] = ProtocolWriteResult(
                        point_id=write.point_id,
                        outcome=WriteOutcome.CONFIRMED_FAILURE,
                        message=str(exc),
                    )
                    continue
                prepared.append((index, point, payload))

            try:
                for start in range(0, len(prepared), self._config.max_subs_per_sum):
                    chunk = prepared[start : start + self._config.max_subs_per_sum]
                    request = self._encode_sum_write(chunk)
                    raw = await asyncio.to_thread(
                        session.run,
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
        mapped: ADSPoint,
        value: object,
    ) -> ProtocolWriteResult:
        """按已解析 index 地址写入单点；配置/取值级问题收敛为单点失败。

        连接/传输级异常原样抛出，由调用方上报，不擅自关闭会话。
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

        session = self._session
        if session is None:
            raise ProtocolError("ADS connection is unavailable")
        try:
            await asyncio.to_thread(
                session.run,
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

    async def interrogate(self, common_address: int | None = None) -> None:
        """ADS 不支持 IEC104 式总召能力。"""
        del common_address
        raise ProtocolCapabilityError("ads does not support interrogation")

    async def unsubscribe(self, handle: SubscriptionHandle) -> None:
        """注销一次原生 Device Notification 订阅（幂等由句柄保证）。"""
        await handle.close()

    async def listen(
        self,
        point_ids: Sequence[str],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> ListenHandle:
        """ADS 的远程数据获取只有原生订阅（subscribe），没有被动监听语义。"""
        del point_ids, callback, interval
        raise ProtocolCapabilityError("ads does not support passive listen — use subscribe")

    async def unlisten(self, handle: ListenHandle) -> None:
        """ADS 不支持被动监听。"""
        del handle
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

    async def _read_sum_raw(
        self,
        session: _AdsSession,
        points: Sequence[ADSPoint],
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        """分批取得全部原始响应后，统一按原始测点顺序解码。"""
        key = tuple(points)
        cached = self._read_cache.get(key)
        if cached is None:
            cached = (self._build_sum_requests(key), self._build_sum_decoders(key))
            if len(self._read_cache) >= 32:
                self._read_cache.pop(next(iter(self._read_cache)))
            self._read_cache[key] = cached
        request, decoders = cached

        responses: list[tuple[Any, bytes]] = []
        for chunk in request:
            response = await asyncio.to_thread(
                session.run,
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
            if not self._connected or self._session is None:
                raise ProtocolError("ADS resolve_symbol requires an active connection")
            group, offset, _ = await self._resolve_symbol_locked(symbol)
            return group, offset

    async def _resolve_symbol_locked(self, symbol: str) -> tuple[int, int, int | None]:
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached
        session = self._session
        if session is None:
            raise ProtocolError("ADS connection is not available")
        info = await asyncio.to_thread(session.run, "get_symbol", symbol)
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
        subscriptions = tuple(self._subscriptions | self._starting_subscriptions)
        self._subscriptions.clear()
        self._starting_subscriptions.clear()
        self._connected = False
        return session, subscriptions


def _decode_sum_write_errors(raw: object, count: int) -> tuple[int, ...]:
    """从 Sum Write 响应解出逐点 ADS 错误码。

    pyads 对无读取类型的 Sum Write 实际返回 ctypes ``c_ubyte`` 数组
    （并非 bytes，且 ctypes.Array 不注册为 collections.abc.Sequence），
    因此按结构化序列能力校验。
    """
    if (
        raw is None
        or isinstance(raw, str)
        or not (hasattr(raw, "__len__") and hasattr(raw, "__getitem__"))
    ):
        raise ProtocolError("ADS sum write returned an invalid response size")
    values = cast("Sequence[int]", raw)
    if len(values) != count * 4:
        raise ProtocolError("ADS sum write returned an invalid response size")
    return tuple(error for (error,) in struct.iter_unpack("<I", bytes(values)))


def _encode_value(value: float | int | bool | str, point: ADSPoint) -> bytes:
    """把单个 ADS 标量序列化为寻址宽度恰好一致的字节串。"""
    if point.data_type == "STRING":
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
        if point.size <= 0 or len(encoded) >= point.size:
            raise ValueError(f"ADS STRING '{point.point_id}' exceeds configured size")
        return encoded.ljust(point.size, b"\x00")
    datatype = _plc_datatype(point.data_type)
    raw = bytes(datatype(value))
    if len(raw) != point.size:
        raise ValueError(f"ADS point '{point.point_id}' data width mismatch")
    return raw


def _as_point_scalar(
    value: object,
) -> float | int | bool | str | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported ADS value type '{type(value).__name__}'")


def _bad_sample(point_id: str) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=None,
        timestamp=datetime.now(UTC),
        quality=Quality.BAD,
    )


def _is_point_level_error(exc: BaseException) -> bool:
    ads_error = getattr(pyads, "ADSError", None)
    return (
        ads_error is not None
        and isinstance(exc, ads_error)
        and getattr(exc, "err_code", None) == _ADSERR_SYMBOL_NOT_FOUND
    )


_ADS_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "SINT": (-128, 127),
    "USINT": (0, 255),
    "INT": (-32768, 32767),
    "UINT": (0, 65535),
    "DINT": (-2147483648, 2147483647),
    "UDINT": (0, 4294967295),
    "LINT": (-9223372036854775808, 9223372036854775807),
    "ULINT": (0, 18446744073709551615),
}


def _coerce_write_value(
    value: object,
    ads_type: str,
) -> float | int | bool | str:
    """把共享标量严格收敛到 ADS 基础类型。"""
    if ads_type == "BOOL":
        if type(value) is not bool:
            raise TypeError("ADS BOOL requires bool value")
        return value

    if ads_type == "STRING":
        if not isinstance(value, str):
            raise TypeError("ADS STRING requires string value")
        return value

    integer_range = _ADS_INTEGER_RANGES.get(ads_type)
    if integer_range is not None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"ADS {ads_type} requires integer-compatible value")
        integer = int(value)
        if float(value) != float(integer):
            raise ValueError(f"ADS {ads_type} requires an integer value")
        lower, upper = integer_range
        if not lower <= integer <= upper:
            raise ValueError(f"ADS {ads_type} value {integer} outside range {lower}..{upper}")
        return integer

    if ads_type in {"REAL", "LREAL"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
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
        subscription.deactivate()
        # DEL_DEVICE_NOTIFICATION 是共享 pyads 连接上的命令，与
        # health/读写互斥，统一经 Driver 的 IO 锁串行；Worker 取消放锁外，
        # 避免用户回调内再做驱动 IO 时自锁。
        async with owner._lock:
            owner._subscriptions.discard(subscription)
            await subscription.deregister()
        await subscription.stop_worker()
