"""基于 c104/lib60870-C 的共享 IEC104 ProtocolPort Adapter。

Shared Core 只提供单连接 connect/read/write/health 和显式总召能力。持续采集、
订阅分发、Task 生命周期和重连策略由上层应用负责。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import c104

from core.application.errors import ConfigError, ProtocolCapabilityError, ProtocolError
from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
    WritableScalar,
    WriteOutcome,
)
from core.domain import ConnectionEndpoint, PointAccess, PointTable, ProtocolOptions

from .codec import command_to_c104, sample_from_c104
from .config import IEC104Config, parse_iec104_config
from .mapping import (
    IEC104Point,
    build_iec104_index,
    validate_iec104_write_type,
)

logger = logging.getLogger(__name__)


class _IoaLockEntry:
    """per-IOA 锁与使用者计数（当前持有者 + 已登记等待者）。"""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


def _register_ioa_lock(
    registry: dict[int, _IoaLockEntry],
    ioa: int,
) -> asyncio.Lock:
    """登记一次 IOA 锁使用并返回锁；必须在第一次 ``await`` 之前调用。

    单事件循环内，登记所在的同步代码段不会被其他协程打断，因此
    「查表 + 计数 +1」相对其他登记/释放操作是原子的：同一 IOA 的所有
    并发使用者始终拿到同一把锁。
    """
    entry = registry.get(ioa)
    if entry is None:
        entry = _IoaLockEntry()
        registry[ioa] = entry
    entry.refs += 1
    return entry.lock


def _release_ioa_lock(
    registry: dict[int, _IoaLockEntry],
    ioa: int,
    lock: asyncio.Lock,
) -> None:
    """释放一次 IOA 锁使用；计数归零（无持有者也无等待者）后删除条目。

    调用点位于 ``finally`` 的同步代码段（无 await），与登记操作互不
    打断，因此计数归零时不可能存在尚未登记的未来使用者，可安全删除。
    取消、超时、异常路径都经 ``finally`` 释放，不会泄漏计数。
    """
    entry = registry.get(ioa)
    if entry is None or entry.lock is not lock:
        return
    entry.refs -= 1
    if entry.refs == 0:
        del registry[ioa]


_SUBSCRIPTION_QUEUE_CAPACITY = 1000


class _Subscription:
    """一个订阅使用一个有界 FIFO 队列和一个长期运行的 Worker。"""

    def __init__(
        self,
        registry: _SubscriptionRegistry,
        callback: Callable[[ProtocolSample], Awaitable[None]],
        ioas: tuple[int, ...] | None,
    ) -> None:
        self._registry = registry
        self._callback = callback
        self._ioas = ioas
        self._closed = False
        self._queue: asyncio.Queue[ProtocolSample] = asyncio.Queue(
            maxsize=_SUBSCRIPTION_QUEUE_CAPACITY
        )
        self._dropped_count = 0
        self._worker: asyncio.Task[None] = asyncio.create_task(
            self._drain(), name="iec104-subscription-worker"
        )

    async def close(self, *, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._registry.unsubscribe(self)
        self._worker.cancel()
        if self._worker is not asyncio.current_task():
            try:
                await asyncio.wait_for(self._worker, timeout=timeout)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                logger.warning("IEC104 subscription worker did not stop in %.3fs", timeout)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()

    def schedule(self, sample: ProtocolSample) -> None:
        """调用方已经在事件循环线程；队列满时丢弃最旧的数据。"""
        if self._closed:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self._queue.task_done()
            self._dropped_count += 1
            if self._dropped_count == 1 or self._dropped_count % 1000 == 0:
                logger.warning(
                    "IEC104 subscription queue dropped %d samples", self._dropped_count
                )
        self._queue.put_nowait(sample)

    async def _drain(self) -> None:
        while True:
            sample = await self._queue.get()
            try:
                await self._callback(sample)
            except Exception:
                logger.exception(
                    "IEC104 subscriber callback failed for point '%s'", sample.point_id
                )
            finally:
                self._queue.task_done()


class _SubscriptionRegistry:
    """仅由 IEC104Driver 所属 asyncio loop 访问的订阅表。"""

    def __init__(self) -> None:
        self._global: list[_Subscription] = []
        self._ioa: dict[int, list[_Subscription]] = {}

    def subscribe(
        self,
        ioas: tuple[int, ...] | None,
        callback: Callable[[ProtocolSample], Awaitable[None]],
    ) -> _Subscription:
        subscription = _Subscription(self, callback, ioas)
        if ioas is None:
            self._global.append(subscription)
            return subscription
        for ioa in dict.fromkeys(ioas):
            self._ioa.setdefault(ioa, []).append(subscription)
        return subscription

    def unsubscribe(self, subscription: _Subscription) -> None:
        if subscription._ioas is None:
            with contextlib.suppress(ValueError):
                self._global.remove(subscription)
            return
        for ioa in dict.fromkeys(subscription._ioas):
            subscriptions = self._ioa.get(ioa)
            if subscriptions is None:
                continue
            with contextlib.suppress(ValueError):
                subscriptions.remove(subscription)
            if not subscriptions:
                del self._ioa[ioa]

    async def close_all(self, *, timeout: float = 5.0) -> None:
        """逐个限时注销订阅，避免单个回调阻塞全部关闭。"""
        subscriptions = list(self._global)
        for items in self._ioa.values():
            for subscription in items:
                if subscription not in subscriptions:
                    subscriptions.append(subscription)
        for subscription in subscriptions:
            await subscription.close(timeout=timeout)


class IEC104Driver:
    """单个 Endpoint 的 IEC104 主站 Driver。

    c104 负责 APCI/ASDU 通信。Driver 不缓存业务点值，重连与业务恢复由外部负责。
    """

    def __init__(
        self,
        endpoint: ConnectionEndpoint,
        point_table: PointTable,
        protocol_options: ProtocolOptions,
    ) -> None:
        if point_table.protocol.name != "iec104":
            raise ConfigError(
                f"point table '{point_table.point_table_id}' protocol is "
                f"'{point_table.protocol.name}', expected 'iec104'"
            )

        self._point_table_id = point_table.point_table_id
        self._config: IEC104Config = parse_iec104_config(
            endpoint,
            protocol_options,
        )
        self._points_by_id, self._points_by_ioa = build_iec104_index(
            list(point_table.points.values()),
        )
        self._validate_write_types(point_table)

        self._lock = asyncio.Lock()
        self._client: Any = None
        self._connection: Any = None
        self._station: Any = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._open_event: asyncio.Event | None = None
        self._is_open = False
        self._closed = True

        # 动态 IOA 接收关联（协议会话级，不是 PointTable 登记）：任何动态点
        # 操作按需登记，使上报和总召响应能路由到订阅者；跨断连保留，重连后
        # 由 on_new_point 重新挂接接收回调。身份策略：PointTable 点优先，
        # 否则保留首个登记的动态 point_id；单次读取按输入对象重标身份。

        self._interrogation_lock = asyncio.Lock()
        self._interrogation_waiter: asyncio.Future[None] | None = None
        self._command_locks: dict[int, _IoaLockEntry] = {}
        self._active_read_locks: dict[int, _IoaLockEntry] = {}
        self._active_reads: dict[int, asyncio.Future[ProtocolSample]] = {}
        self._receive_callback_factory: Any = None
        self._subscriptions = _SubscriptionRegistry()

    def update_point_table(self, point_table: PointTable) -> None:
        """热重载点表：重建 IOA 寻址索引（不断开连接）。

        已注册的 c104 通知点由上层重启订阅后按新映射重新注册。
        """
        next_by_id, next_by_ioa = build_iec104_index(
            list(point_table.points.values()),
        )
        for point in point_table.points.values():
            if point.access in (PointAccess.WRITE, PointAccess.READ_WRITE):
                validate_iec104_write_type(next_by_id[point.point_id])
        # 验证完成后统一替换映射，旧映射下的待处理读取将被拒绝。
        self._point_table_id = point_table.point_table_id
        self._points_by_id, self._points_by_ioa = next_by_id, next_by_ioa
        self._reject_pending_reads("IEC104 point table changed")

    def _validate_write_types(self, point_table: PointTable) -> None:
        """校验全部可写点的命令类型（与构造期校验同一规则）。"""
        for point in point_table.points.values():
            mapped = self._points_by_id[point.point_id]
            if point.access in (PointAccess.WRITE, PointAccess.READ_WRITE):
                validate_iec104_write_type(mapped)

    def capabilities(self) -> frozenset[ProtocolCapability]:
        """返回 IEC104 Driver 实际支持的协议能力。"""
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.SUBSCRIBE,
                ProtocolCapability.INTERROGATE,
            }
        )

    async def connect(self) -> None:
        """创建 c104 client 并等待连接进入 OPEN。"""
        async with self._lock:
            if self._client is not None:
                return

            self._closed = False
            self._is_open = False
            self._loop = asyncio.get_running_loop()
            self._open_event = asyncio.Event()

            client = c104.Client(command_timeout_ms=int(self._config.t1 * 2 * 1000))
            connection = client.add_connection(
                ip=self._config.host,
                port=self._config.port,
                init=c104.Init.NONE,
            )
            if connection is None:
                self._closed = True
                raise ProtocolError(
                    f"IEC104 invalid endpoint " f"{self._config.host}:{self._config.port}"
                )

            self._apply_protocol_parameters(connection)
            station = connection.add_station(common_address=self._config.common_addr)
            if station is None:
                self._closed = True
                raise ProtocolError(f"IEC104 invalid common_addr={self._config.common_addr}")

            from .callbacks import (
                new_point_callback,
                raw_receive_callback,
                receive_callback,
                state_callback,
            )

            connection.on_state_change(callable=state_callback(self._handle_state_change))
            connection.on_receive_raw(callable=raw_receive_callback(self._handle_raw_receive))
            client.on_new_point(callable=new_point_callback(self._handle_new_point))
            self._receive_callback_factory = receive_callback

            self._client = client
            self._connection = connection
            self._station = station

            try:
                # c104 ``Client.start()`` 已经连接全部 connection 并维护自动
                # 重连；不要再调 ``connection.connect()``——它与 c104 内部
                # 线程的 connect/reconnect 共用未串行化的状态机，属于多余调用。
                await asyncio.to_thread(client.start)
                await asyncio.wait_for(
                    self._open_event.wait(),
                    timeout=self._config.t0,
                )
            except TimeoutError:
                await self._cleanup_failed_connect()
                raise ProtocolError(
                    f"IEC104 connect to {self._config.host}:"
                    f"{self._config.port} timed out after {self._config.t0}s"
                ) from None
            except Exception as exc:
                await self._cleanup_failed_connect()
                raise ProtocolError(
                    f"IEC104 connect failed for {self._config.host}:" f"{self._config.port}: {exc}"
                ) from exc

            self._is_open = True

    async def close(self) -> None:
        """关闭连接与 c104 client；重复调用安全。"""
        async with self._lock:
            if self._closed and self._client is None:
                return

            self._closed = True
            self._is_open = False
            client, connection = self._client, self._connection
            self._client = None
            self._connection = None
            self._station = None
            self._open_event = None
            self._receive_callback_factory = None
            self._reject_pending_reads("IEC104 connection closed")
            self._reject_interrogation("IEC104 connection closed")
            self._loop = None

        await self._subscriptions.close_all(timeout=self._config.t1)

        # 不要显式调 ``connection.disconnect()``：它会持有 GIL 等待 c104
        # 连接线程退出，而连接线程分发 Python 回调时需要同一把 GIL，c104 的
        # GilAwareMutex 100ms 死锁保险丝会以 std::terminate 结束整个进程。
        # ``Client.stop()`` 自身在释放 GIL 的状态下完成全部 disconnect 与
        # 线程回收，是并发流量下唯一安全的拆除路径。
        del connection
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.stop)

    def is_open(self) -> bool:
        """IEC104 连接是否处于 OPEN 状态（c104 协议级握手完成）。"""
        return self._is_open

    async def health(self) -> ConnectionHealth:
        """返回 c104 APCI 状态机维护的连接健康结果。

        c104 使用配置的 t3 keep-alive/TESTFR 与连接状态回调维护 OPEN 状态；
        Watchdog 只消费该协议栈状态，不读取本地数据镜像，也不重复实现
        IEC104 心跳。
        """
        if self._is_open:
            return ConnectionHealth(
                healthy=True,
                message=(
                    f"IEC104 OPEN {self._config.host}:{self._config.port}; "
                    f"keep_alive={self._config.t3}s"
                ),
            )
        if self._client is None:
            return ConnectionHealth(healthy=False, message="not connected")
        return ConnectionHealth(
            healthy=False,
            message="IEC104 connection is not OPEN",
        )

    async def read_one(self, point: str) -> ProtocolSample:
        """发送 C_RD_NA_1，并等待指定 IOA 的本次读取响应。"""
        if not self._is_open:
            raise ProtocolError("IEC104 read requires an OPEN connection")
        mapped = self._resolve_point(point)
        lock = _register_ioa_lock(self._active_read_locks, mapped.ioa)
        try:
            async with lock:
                loop = asyncio.get_running_loop()
                future: asyncio.Future[ProtocolSample] = loop.create_future()
                self._active_reads[mapped.ioa] = future
                try:
                    await self._send_read(mapped)
                    try:
                        sample = await asyncio.wait_for(future, timeout=self._config.t1)
                    except TimeoutError as exc:
                        raise ProtocolError(
                            f"IEC104 active read timed out for {mapped.point_id}"
                        ) from exc
                    return replace(sample, point_id=mapped.point_id)
                finally:
                    if self._active_reads.get(mapped.ioa) is future:
                        self._active_reads.pop(mapped.ioa, None)
        finally:
            _release_ioa_lock(self._active_read_locks, mapped.ioa, lock)

    async def write_one(
        self,
        write: ProtocolWrite,
    ) -> ProtocolWriteResult:
        """执行单点遥控/设点，并按 ACT_CON 结果返回状态。

        统一接收 ProtocolWrite；point 为 ID 或 IEC104Point。动态点按
        自身 IOA 和命令 type_id 寻址，不要求预先登记。
        """
        mapped = self._resolve_point(write.point)
        write_value = write.value
        if not self._is_open or self._station is None:
            raise ProtocolError("IEC104 write requires an OPEN connection")
        return await self._execute_write(mapped, write_value)

    async def read_many(
        self,
        points: Sequence[str],
    ) -> tuple[ProtocolSample, ...]:
        """IEC104 不支持任意 IOA 集合的批量读取。"""
        raise ProtocolCapabilityError("iec104 does not support read_many")

    async def write_many(
        self,
        writes: Sequence[ProtocolWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """IEC104 不支持原子批量控制命令。"""
        raise ProtocolCapabilityError("iec104 does not support write_many")

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: Callable[[ProtocolSample], Awaitable[None]],
        *,
        interval: float | None = None,
    ) -> _Subscription:
        """注册 IEC104 主动上送/总召响应的样本回调。

        空 point_ids 表示订阅本 PointTable 内的全部已知 IOA。动态
        IEC104Point 按自身 IOA 订阅并登记接收关联。生命周期由调用方
        持有返回句柄；订阅不自动触发总召。
        """
        del interval
        if point_ids:
            mapped = tuple(self._resolve_point(point) for point in point_ids)
            for point in mapped:
                self._register_dynamic_ioa(point)
            ioas = tuple(point.ioa for point in mapped)
        else:
            ioas = None
        return self._subscriptions.subscribe(ioas, callback)

    async def _send_read(self, mapped: IEC104Point) -> None:
        """发送单点读取请求；调用方负责等待 REQUEST 响应。"""
        if not self._is_open or self._station is None:
            raise ProtocolError("IEC104 active read requires an OPEN connection")
        point = self._station.get_point(mapped.ioa)
        if point is None:
            point = self._register_monitoring_point(mapped)
        try:
            accepted = await asyncio.to_thread(point.read)
        except Exception as exc:
            raise ProtocolError(f"IEC104 active read failed for {mapped.point_id}: {exc}") from exc
        if not accepted:
            raise ProtocolError(f"IEC104 active read rejected for {mapped.point_id}")

    def _reject_pending_reads(self, reason: str) -> None:
        pending = tuple(self._active_reads.values())
        self._active_reads.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ProtocolError(reason))

    async def interrogate(self) -> None:
        """等待本次站总召的 ACT_TERM，而非仅等待发送/激活确认。

        返回值只表示协议级总召流程完成，不保证每个业务测点都已上报。
        """
        async with self._interrogation_lock:
            if not self._is_open or self._connection is None:
                raise ProtocolError("IEC104 interrogation requires an OPEN connection")
            loop = asyncio.get_running_loop()
            completed: asyncio.Future[None] = loop.create_future()
            self._interrogation_waiter = completed
            try:
                try:
                    accepted = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._connection.interrogation,
                            common_address=self._config.common_addr,
                            cause=c104.Cot.ACTIVATION,
                            qualifier=c104.Qoi.STATION,
                            wait_for_response=True,
                        ),
                        timeout=self._config.t1 * 2,
                    )
                except TimeoutError as exc:
                    raise ProtocolError("IEC104 interrogation activation timed out") from exc
                except Exception as exc:
                    raise ProtocolError(f"IEC104 interrogation failed: {exc}") from exc
                if not accepted:
                    raise ProtocolError("IEC104 general interrogation rejected")
                try:
                    await asyncio.wait_for(completed, timeout=self._config.t1 * 2)
                except TimeoutError as exc:
                    raise ProtocolError("IEC104 interrogation ACT_TERM timed out") from exc
            finally:
                if self._interrogation_waiter is completed:
                    self._interrogation_waiter = None
                if completed.done() and not completed.cancelled():
                    completed.exception()
                else:
                    completed.cancel()

    def _handle_raw_receive(self, connection: Any, data: bytes) -> None:
        """识别站总召的否定 ACT_CON 或 ACT_TERM，不重复解码测点。"""
        if self._closed or connection is not self._connection:
            return
        result = self._interrogation_result(data, self._config.common_addr)
        if result is None:
            return
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._finish_interrogation, result)

    @staticmethod
    def _interrogation_result(data: bytes, common_addr: int) -> str | None:
        """从单个完整 I-frame APDU 中提取 C_IC_NA_1 的控制结果。"""
        # IEC104: APCI(6), TypeID(1), VSQ(1), COT(2), CA(2),
        # IOA(3), QOI(1)。任何其他帧均不是本次总召控制报文。
        if (
            len(data) < 16
            or data[0] != 0x68
            or data[1] + 2 != len(data)
            or data[2] & 0x01
            or data[6] != 100
            or data[7] != 1
            or int.from_bytes(data[10:12], "little") != common_addr
            or int.from_bytes(data[12:15], "little") != 0
            or data[15] != 20
        ):
            return None
        cot = data[8] & 0x3F
        if cot == 7 and data[8] & 0x40:
            return "rejected"
        if cot == 10:
            return "rejected" if data[8] & 0x40 else "completed"
        return None

    def _finish_interrogation(self, result: str) -> None:
        waiter = self._interrogation_waiter
        if waiter is None or waiter.done():
            return
        if result == "completed":
            waiter.set_result(None)
        else:
            waiter.set_exception(
                ProtocolError("IEC104 general interrogation negative confirmation")
            )

    def _reject_interrogation(self, reason: str) -> None:
        waiter = self._interrogation_waiter
        self._interrogation_waiter = None
        if waiter is not None and not waiter.done():
            waiter.set_exception(ProtocolError(reason))

    async def _execute_write(
        self,
        mapped: IEC104Point,
        write_value: object,
    ) -> ProtocolWriteResult:
        lock = _register_ioa_lock(self._command_locks, mapped.ioa)

        try:
            return await self._execute_write_locked(mapped, write_value, lock)
        finally:
            _release_ioa_lock(self._command_locks, mapped.ioa, lock)

    async def _execute_write_locked(
        self,
        mapped: IEC104Point,
        write_value: object,
        lock: asyncio.Lock,
    ) -> ProtocolWriteResult:
        async with lock:
            try:
                point_type, value = command_to_c104(
                    mapped,
                    write_value,
                )
                point = self._get_or_create_command_point(
                    mapped.ioa,
                    point_type,
                )
            except (ConfigError, ProtocolError, TypeError, ValueError) as exc:
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.CONFIRMED_FAILURE,
                    message=str(exc),
                )

            point.value = value
            started = time.monotonic()
            try:
                accepted = await asyncio.to_thread(
                    point.transmit,
                    c104.Cot.ACTIVATION,
                )
            except Exception as exc:
                # transmit 异常（含等待 ACT_CON 期间的连接故障）：命令
                # 可能已送达设备，无法确证最终结果。
                return ProtocolWriteResult(
                    point_id=mapped.point_id,
                    outcome=WriteOutcome.UNKNOWN,
                    message=str(exc),
                )
            elapsed = time.monotonic() - started

        if accepted:
            return ProtocolWriteResult(
                point_id=mapped.point_id,
                outcome=WriteOutcome.CONFIRMED_SUCCESS,
            )
        # c104 transmit 只返回 bool，不区分否定确认、超时与断线；
        # 没有肯定确认证据时一律 UNKNOWN——命令可能已被设备执行，
        # 绝不伪报为确证未执行。
        return ProtocolWriteResult(
            point_id=mapped.point_id,
            outcome=WriteOutcome.UNKNOWN,
            message=self._command_not_confirmed_reason(elapsed),
        )

    def _get_or_create_command_point(
        self,
        ioa: int,
        point_type: Any,
    ) -> Any:
        station = self._station
        if station is None:
            raise ProtocolError("IEC104 station is not available")

        existing = station.get_point(ioa)
        if existing is not None:
            if existing.type == point_type:
                return existing
            raise ProtocolError(
                f"IEC104 IOA {ioa} already registered as "
                f"{existing.type.name}, cannot send {point_type.name}"
            )

        point = station.add_point(
            io_address=ioa,
            type=point_type,
        )
        if point is None:
            raise ProtocolError(f"IEC104 cannot create command point at IOA {ioa}")
        return point

    def _mapped_point(self, point_id: str) -> IEC104Point:
        mapped = self._points_by_id.get(point_id)
        if mapped is None:
            raise ConfigError(
                f"point '{point_id}' is not part of connection " f"'{self._point_table_id}'"
            )
        return mapped

    def _resolve_point(self, point: str) -> IEC104Point:
        """仅允许按当前 PointTable 已配置的 Key 寻址。"""
        if not isinstance(point, str):
            raise ConfigError("protocol point must be a configured point key")
        return self._mapped_point(point)

    def _ioa_mapping(self, ioa: int) -> IEC104Point | None:
        """IOA 的规范身份映射：PointTable 点优先，其次动态登记。"""
        return self._points_by_ioa.get(ioa)

    def _register_monitoring_point(self, mapped: IEC104Point) -> Any:
        """为动态点主动读做 c104 内部注册（协议会话对象，非 PointTable 登记）。

        c104 要求本地 Point 存在才能发送单点读；必须给出监测类型（M_*）
        type_id 才能注册——类型未知时不伪造注册，如实拒绝。
        """
        station = self._station
        if station is None:
            raise ProtocolError("IEC104 station is not available")
        type_id = mapped.type_id
        point_type = getattr(c104.Type, type_id, None) if type_id is not None else None
        if point_type is None or not str(type_id).startswith("M_"):
            raise ProtocolError(
                f"IEC104 IOA {mapped.ioa} is not registered locally; dynamic "
                "active read requires a monitoring type_id (M_*)"
            )
        point = station.add_point(io_address=mapped.ioa, type=point_type)
        if point is None:
            raise ProtocolError(f"IEC104 cannot create point at IOA {mapped.ioa}")
        factory = self._receive_callback_factory
        if factory is not None:
            point.on_receive(callable=factory(self._handle_point_receive))
        return point

    def _apply_protocol_parameters(self, connection: Any) -> None:
        params = connection.protocol_parameters
        cfg = self._config
        params.connection_timeout = int(cfg.t0)
        params.message_timeout = int(cfg.t1)
        params.confirm_interval = int(cfg.t2)
        params.keep_alive_interval = int(cfg.t3)
        params.send_window_size = cfg.k
        params.receive_window_size = cfg.w

    async def _cleanup_failed_connect(self) -> None:
        self._closed = True
        self._is_open = False
        client = self._client
        self._client = None
        self._connection = None
        self._station = None
        self._open_event = None
        self._receive_callback_factory = None
        self._loop = None

        # 与 close() 同理：拆除只经 ``Client.stop()``（全程释放 GIL），
        # 不做显式 ``connection.disconnect()``。
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.stop)

    def _command_not_confirmed_reason(self, elapsed: float) -> str:
        if not self._is_open:
            return "activation not confirmed: connection lost"
        if elapsed >= self._config.t1 * 2 * 0.9:
            return "activation not confirmed: timeout"
        return "activation not confirmed: rejected or link loss"

    def _handle_state_change(self, state: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(
                self._handle_state,
                state,
            )

    def _handle_state(self, state: Any) -> None:
        if self._closed:
            return
        if state == c104.ConnectionState.OPEN:
            self._is_open = True
            if self._open_event is not None:
                self._open_event.set()
            return

        self._is_open = False
        self._reject_pending_reads("IEC104 connection lost")
        self._reject_interrogation("IEC104 connection lost")

    def _handle_new_point(
        self,
        station: Any,
        io_address: int,
        point_type: Any,
    ) -> None:
        if self._closed:
            return
        if io_address not in self._points_by_ioa :
            return

        factory = self._receive_callback_factory
        if factory is None:
            return

        point = station.get_point(io_address)
        if point is None:
            point = station.add_point(
                io_address=io_address,
                type=point_type,
            )
        if point is None:
            return
        registered_type = getattr(point, "type", point_type)
        if registered_type != point_type:
            logger.error(
                "IEC104 IOA %s TypeID mismatch: received %s, registered %s; "
                "the existing c104 Point cannot safely decode the new type",
                io_address,
                point_type,
                registered_type,
            )
            return
        point.on_receive(callable=factory(self._handle_point_receive))

    def _handle_point_receive(self, point: Any, message: Any) -> Any:
        ioa = point.io_address
        mapped = self._ioa_mapping(ioa)
        if mapped is None:
            return c104.ResponseState.NONE

        try:
            sample = sample_from_c104(
                point,
                mapped.point_id,
            )
        except (TypeError, ValueError):
            sample = ProtocolSample(
                point_id=mapped.point_id,
                value=None,
                quality=Quality.BAD,
                timestamp=datetime.now(UTC),
            )

        loop = self._loop
        if loop is not None and not self._closed:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    self._store_sample,
                    ioa,
                    sample,
                    message.cot == c104.Cot.REQUEST,
                    bool(message.is_negative),
                )
        return c104.ResponseState.NONE

    def _store_sample(
        self,
        ioa: int,
        sample: ProtocolSample,
        requested: bool = False,
        negative: bool = False,
    ) -> None:
        if self._closed or not self._is_open:
            return
        mapped = self._ioa_mapping(ioa)
        if mapped is None or mapped.point_id != sample.point_id:
            # A queued callback may belong to the previous point-table generation.
            return
        if negative and not requested:
            return
        if negative and requested:
            waiter = self._active_reads.get(ioa)
            if waiter is not None and not waiter.done():
                waiter.set_exception(ProtocolError(f"IEC104 negative read response at IOA {ioa}"))
            return
        if requested:
            waiter = self._active_reads.get(ioa)
            if waiter is not None and not waiter.done():
                waiter.set_result(sample)
        for subscription in (*self._subscriptions._global, *self._subscriptions._ioa.get(ioa, [])):
            subscription.schedule(sample)
