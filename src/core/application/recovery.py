"""Shared protocol read/write recovery boundary.

职责划分：

- Driver：单次 connect/read/write，上报本地连接状态（``is_open``）与最近
  通信结果（``health``），不重连、不重试、不因普通通信异常关闭连接。
- RecoveringProtocol（本类）：I/O 前确保连接打开，读取失败后的等待与
  重试，操作超时边界，并发恢复互斥，重连事件通知，关闭与取消安全。

重试语义：

- 读取（``read_one``/``read_many``，幂等）：连接失败与可恢复的通信失败
  进入同一重试循环，由 ``read_retries`` 控制首次尝试失败后的额外重试
  次数——``0`` 不重试，``-1`` 无限重试（直至成功、取消或 close）。
- 写入（``write_one``/``write_many``）：独立策略——发送前最多执行一次
  必要的 connect；请求一旦可能到达设备，超时或断开均绝不自动重发。
- 配置、参数、协议能力与数据级错误（含 Modbus 异常响应）不可恢复，
  立即抛出，不消耗重试预算。
"""

from __future__ import annotations  # noqa: I001 - imports follow application layering

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import TypeVar

from core.domain import PointTable

from .errors import ProtocolCapabilityError, ProtocolConnectionError, ProtocolError
from .port import ProtocolPort, ProtocolSampleCallback, SubscriptionHandle
from .protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    WritableScalar,
)


_T = TypeVar("_T")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoverySettings:
    """RecoveringProtocol 的恢复与超时策略。

    Attributes:
        read_retries: 一次逻辑读取在首次尝试失败后允许的额外重试次数；
            ``0`` 不重试，``-1`` 无限重试。首次必要的 connect 与首次
            read 始终允许执行，不消耗重试预算。
        retry_interval: 每次重试前的等待间隔（秒）；允许 ``0``（不等待，
            仍让出事件循环），拒绝负数/NaN/Inf。
        connect_timeout: 单次 connect 的整体边界（秒）。
        read_timeout: 单次 read 的整体边界；``None`` 不加外层边界
            （由协议自身超时兜底）。
        write_timeout: 单次 write 的整体边界；``None`` 同上。
    """

    read_retries: int = 1
    retry_interval: float = 1.0
    connect_timeout: float = 1.0
    read_timeout: float | None = 1.0
    write_timeout: float | None = 1.0

    def __post_init__(self) -> None:
        if type(self.read_retries) is not int or self.read_retries < -1:
            raise ValueError("read_retries must be an integer >= -1")
        if (
            isinstance(self.retry_interval, bool)
            or not isinstance(self.retry_interval, int | float)
            or not isfinite(self.retry_interval)
            or self.retry_interval < 0
        ):
            raise ValueError("retry_interval must be a finite number >= 0")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        if self.read_timeout is not None and self.read_timeout <= 0:
            raise ValueError("read_timeout must be > 0")
        if self.write_timeout is not None and self.write_timeout <= 0:
            raise ValueError("write_timeout must be > 0")


class RecoveringProtocol:
    """Wrap a ProtocolPort without changing underlying protocol semantics."""

    def __init__(self, driver: ProtocolPort, settings: RecoverySettings) -> None:
        self._driver = driver
        self._settings = settings
        self._connect_lock = asyncio.Lock()
        # close 后禁止任何路径重建连接；close_event 用于及时唤醒重试等待。
        self._closed = False
        self._close_event = asyncio.Event()
        self._on_reconnect: Callable[[], None] | None = None

    def set_reconnect_hook(self, hook: Callable[[], None] | None) -> None:
        """Register a listener invoked once per successful transparent reconnect.

        The wrapper reconnects inside read/write when the Driver reports not
        open; callers above the protocol layer (e.g. Collector device
        runtime) otherwise cannot observe these reconnections. The hook is
        invoked synchronously after the connection is re-established;
        hook exceptions are logged and never break the protocol path.
        """
        self._on_reconnect = hook

    def _notify_reconnect(self) -> None:
        if self._on_reconnect is None:
            return
        try:
            self._on_reconnect()
        except Exception:
            logger.warning("reconnect hook failed", exc_info=True)

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self._driver.capabilities()

    def is_open(self) -> bool:
        return self._driver.is_open()

    def health(self) -> ConnectionHealth:
        return self._driver.health()

    def update_point_table(self, point_table: PointTable) -> None:
        self._driver.update_point_table(point_table)

    async def connect(self) -> None:
        """建立连接；已打开时直接返回（幂等），可复用时不做无意义的重连。

        与 I/O 路径内的透明恢复共用同一把锁：上层（如 Collector
        ``DeviceRuntime.ensure_connected``）的主动 connect 与在途的透明
        重连被串行化，同一连接不会被并发重建。显式 connect 解除 close
        的禁止重建状态。
        """
        async with self._connect_lock:
            self._closed = False
            self._close_event.clear()
            if self._driver.is_open():
                return
            await self._connect_locked()

    async def close(self) -> None:
        """关闭连接并禁止后续自动重建；及时终止在途的无限重试。"""
        async with self._connect_lock:
            self._closed = True
            self._close_event.set()
            await self._driver.close()

    async def _bounded(self, op: Awaitable[_T], limit: float | None, label: str) -> _T:
        try:
            return await asyncio.wait_for(op, timeout=limit)
        except TimeoutError as exc:
            raise ProtocolConnectionError(f"{label} timed out after {limit}s") from exc

    async def _connect_locked(self) -> None:
        """不带锁的单次 connect——仅由已持有锁的调用方使用。"""
        await self._bounded(self._driver.connect(), self._settings.connect_timeout, "connect")

    async def _ensure_open(self) -> bool:
        """确保连接打开（必要时执行一次 connect）；返回是否发生了实际连接。

        只在锁内执行连接检查与 connect 本身，不执行业务 I/O 或重试等待。
        """
        if self._closed:
            raise ProtocolError("protocol connection is closed")
        if self._driver.is_open():
            return False
        async with self._connect_lock:
            # 等锁期间其他协程可能已完成恢复或关闭——复查后再行动。
            if self._closed:
                raise ProtocolError("protocol connection is closed")
            if self._driver.is_open():
                return False
            await self._connect_locked()
            return True

    async def _recover_open(self) -> None:
        """I/O 前的连接恢复；实际发生重连时通知上层（每次恢复仅一次）。"""
        if await self._ensure_open():
            self._notify_reconnect()

    def _is_recoverable(self, exc: BaseException) -> bool:
        """判定读取失败是否可重试。

        明确的链路级故障（``ProtocolConnectionError``：传输断开、对端无
        响应、I/O 超时）总是可重试；其余 ``ProtocolError`` 仅在连接已经
        不可用时作为通用兜底可重试（兼容未细分异常类型的 Driver）。
        协议能力错误与设备已正常应答的数据级错误不可重试。
        """
        if isinstance(exc, ProtocolCapabilityError):
            return False
        if isinstance(exc, ProtocolConnectionError | TimeoutError):
            return True
        return not self._driver.is_open()

    def _can_retry(self, retries: int) -> bool:
        limit = self._settings.read_retries
        return limit == -1 or retries < limit

    async def _wait_before_retry(self) -> None:
        """按配置间隔等待；close 立即唤醒，绝不忙循环。"""
        interval = self._settings.retry_interval
        if interval > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._close_event.wait(), interval)
        else:
            await asyncio.sleep(0)

    async def _read_with_recovery(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        """唯一的读取重试循环：连接失败与可恢复读取失败共享同一预算。"""
        retries = 0
        while True:
            try:
                await self._recover_open()
                return await self._bounded(
                    operation(),
                    self._settings.read_timeout,
                    "read",
                )
            except asyncio.CancelledError:
                raise
            except (ProtocolError, TimeoutError) as exc:
                if (
                    self._closed
                    or not self._is_recoverable(exc)
                    or not self._can_retry(retries)
                ):
                    raise
                retries += 1
                await self._wait_before_retry()

    async def read_one(self, point_id: str) -> ProtocolSample:
        return await self._read_with_recovery(lambda: self._driver.read_one(point_id))

    async def read_many(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        # 重试整体重新执行 read_many：Driver 的批量读是全有或全无语义，
        # 只有成功的那一次尝试产生结果，顺序/质量/时间戳不会跨尝试拼接。
        return await self._read_with_recovery(lambda: self._driver.read_many(point_ids))

    async def write_one(
        self,
        write: ProtocolWrite | str,
        value: WritableScalar | None = None,
    ) -> ProtocolWriteResult:
        """写入一个点；发送前最多一次必要 connect，发送后绝不自动重发。

        契约形式为 ``write_one(ProtocolWrite)``；Driver 声明扩展写签名时
        （如 Modbus 动态点 ``write_one(point, value)``），第二参数原样
        转发给 Driver——与 ``request_read_one``/``read_active_one`` 相同
        的可选扩展 API 转发模式，恢复与超时语义不变。
        """
        await self._recover_open()
        coro: Awaitable[ProtocolWriteResult]
        if value is None:
            if not isinstance(write, ProtocolWrite):
                raise TypeError("write_one(point, value) requires an explicit value")
            coro = self._driver.write_one(write)
        else:
            method = getattr(self._driver, "write_one")  # noqa: B009 - Driver 扩展签名
            coro = method(write, value)
        # No retry after a write has begun: PLC/RTU may have applied the command.
        return await self._bounded(coro, self._settings.write_timeout, "write")

    async def write_many(self, writes: Sequence[ProtocolWrite]) -> tuple[ProtocolWriteResult, ...]:
        # 未声明批量写能力的 Driver（如 Modbus）在任何输入下都明确拒绝，
        # 且在连接恢复之前拒绝，避免无意义的重连；绝不降级为逐点写入。
        if ProtocolCapability.WRITE_MANY not in self._driver.capabilities():
            raise NotImplementedError(f"{type(self._driver).__name__} does not support write_many")
        if not writes:
            return ()
        await self._recover_open()
        # No retry after a write has begun: PLC/RTU may have applied the command.
        return await self._bounded(
            self._driver.write_many(writes), self._settings.write_timeout, "write"
        )

    async def subscribe(
        self,
        point_ids: Sequence[str],
        callback: ProtocolSampleCallback,
        *,
        interval: float | None = None,
    ) -> SubscriptionHandle:
        await self._recover_open()
        return await self._driver.subscribe(point_ids, callback, interval=interval)

    async def interrogate(self) -> None:
        await self._recover_open()
        await self._bounded(self._driver.interrogate(), self._settings.read_timeout, "interrogate")

    async def request_read_one(self, point_id: str) -> None:
        await self._recover_open()
        method = getattr(self._driver, "request_read_one")  # noqa: B009 - optional IEC104 API
        await self._bounded(method(point_id), self._settings.read_timeout, "active read")

    async def request_read_many(self, point_ids: Sequence[str]) -> None:
        for point_id in point_ids:
            await self.request_read_one(point_id)

    async def read_active_one(
        self, point_id: str, *, timeout: float | None = None
    ) -> ProtocolSample:
        """IEC104-specific fresh read. Never substitute the local mirror."""
        await self._recover_open()
        method = getattr(self._driver, "read_active_one")  # noqa: B009 - optional IEC104 API
        limit = self._settings.read_timeout if timeout is None else timeout
        return await self._bounded(method(point_id, timeout=limit), limit, "active read")

    async def read_active_many(
        self, point_ids: Sequence[str], *, timeout: float | None = None
    ) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        await self._recover_open()
        method = getattr(self._driver, "read_active_many")  # noqa: B009 - optional IEC104 API
        limit = self._settings.read_timeout if timeout is None else timeout
        return await self._bounded(method(point_ids, timeout=limit), limit, "active read")
