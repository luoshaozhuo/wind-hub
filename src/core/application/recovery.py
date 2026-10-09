"""Shared protocol read/write recovery boundary.

A write is retried ONLY before its first transmission. Once dispatched,
timeouts/disconnects are ambiguous and NEVER trigger automatic retransmission.
"""

from __future__ import annotations  # noqa: I001 - imports follow application layering

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from core.domain import PointTable

from .errors import ProtocolError
from .port import ProtocolPort, ProtocolSampleCallback, SubscriptionHandle
from .protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
)


_T = TypeVar("_T")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoverySettings:
    # reconnect_attempts 是一次逻辑 I/O 操作（含读前恢复与读后失败再恢复）
    # 允许执行的连接恢复尝试总次数；0 表示禁止自动重连。
    reconnect_attempts: int = 1
    connect_timeout: float = 10.0
    read_timeout: float | None = 5.0
    write_timeout: float | None = 5.0

    def __post_init__(self) -> None:
        if type(self.reconnect_attempts) is not int or self.reconnect_attempts < 0:
            raise ValueError("reconnect_attempts must be a nonnegative integer")
        if self.connect_timeout <= 0:
            raise ValueError("connect_timeout must be > 0")
        if self.read_timeout is not None and self.read_timeout <= 0:
            raise ValueError("read_timeout must be > 0")
        if self.write_timeout is not None and self.write_timeout <= 0:
            raise ValueError("write_timeout must be > 0")


class RecoveryPort:
    """Wrap a ProtocolPort without changing underlying protocol semantics."""

    def __init__(self, driver: ProtocolPort, settings: RecoverySettings) -> None:
        self._driver = driver
        self._settings = settings
        self._connect_lock = asyncio.Lock()
        self._on_reconnect: Callable[[], None] | None = None

    def set_reconnect_hook(self, hook: Callable[[], None] | None) -> None:
        """Register a listener invoked once per successful transparent reconnect.

        The wrapper reconnects inside read/write when the Driver reports
        unhealthy; callers above the protocol layer (e.g. Collector device
        runtime) otherwise cannot observe these reconnections. The hook is
        invoked synchronously after the connection is verified healthy;
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

    def health(self) -> ConnectionHealth:
        return self._driver.health()

    def update_point_table(self, point_table: PointTable) -> None:
        self._driver.update_point_table(point_table)

    async def connect(self) -> None:
        """建立连接；已健康时直接返回（幂等）。

        与读路径内的透明恢复共用同一把锁：上层（如 Collector
        ``DeviceRuntime.ensure_connected``）的主动 connect 与在途的透明
        重连被串行化，同一连接不会被并发重建。
        """
        async with self._connect_lock:
            if self.health().healthy:
                return
            await self._connect_once()

    async def _connect_once(self) -> None:
        """不带锁与快路径的单次 connect——仅由已持有锁的调用方使用。"""
        await self._bounded(self._driver.connect(), self._settings.connect_timeout, "connect")

    async def close(self) -> None:
        """关闭连接；与在途的连接恢复串行，保证恢复不会晚于 close 重建连接。"""
        async with self._connect_lock:
            await self._driver.close()

    async def _bounded(self, op: Awaitable[_T], limit: float | None, label: str) -> _T:
        try:
            return await asyncio.wait_for(op, timeout=limit)
        except TimeoutError as exc:
            raise ProtocolError(f"{label} timed out after {limit}s") from exc

    async def _restore_if_needed(self, budget: int | None = None) -> int:
        """连接不健康时按预算恢复；返回本次逻辑操作剩余的重连预算。

        ``reconnect_attempts`` 是**一次逻辑 I/O 操作**的总恢复预算——调用方
        （如读路径的「读前恢复 + 读后失败再恢复」）共享同一份预算，而不是
        每次调用各自获得完整预算。``budget=None`` 表示以配置值启动一份
        新预算。
        """
        remaining = self._settings.reconnect_attempts if budget is None else budget
        if self.health().healthy:
            return remaining
        async with self._connect_lock:
            if self.health().healthy:
                return remaining
            last_error: Exception | None = None
            while remaining > 0:
                remaining -= 1
                try:
                    # Explicitly dispose stale sockets/session handles.
                    await self._bounded(
                        self._driver.close(), self._settings.connect_timeout, "close"
                    )
                    await self._connect_once()
                    if self.health().healthy:
                        self._notify_reconnect()
                        return remaining
                    last_error = ProtocolError("connection not healthy after connect")
                except Exception as exc:
                    last_error = exc
            raise ProtocolError(
                f"connection unavailable after {self._settings.reconnect_attempts} "
                f"reconnect attempt(s): {last_error}"
            ) from last_error

    async def _read_with_recovery(self, action: Callable[[], Awaitable[_T]]) -> _T:
        remaining = await self._restore_if_needed()
        try:
            return await self._bounded(action(), self._settings.read_timeout, "read")
        except (ProtocolError, TimeoutError):
            # Reads are idempotent. Retry only if the Driver declares itself
            # disconnected, never on application/point-level errors.
            if self.health().healthy or remaining <= 0:
                raise
            await self._restore_if_needed(remaining)
            return await self._bounded(action(), self._settings.read_timeout, "read")

    async def read_one(self, point_id: str) -> ProtocolSample:
        return await self._read_with_recovery(lambda: self._driver.read_one(point_id))

    async def read_many(
        self, point_ids: Sequence[str]
    ) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        return await self._read_with_recovery(lambda: self._driver.read_many(point_ids))

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        await self._restore_if_needed()
        # No retry after a write has begun: PLC/RTU may have applied the command.
        return await self._bounded(
            self._driver.write_one(write), self._settings.write_timeout, "write"
        )

    async def write_many(
        self, writes: Sequence[ProtocolWrite]
    ) -> tuple[ProtocolWriteResult, ...]:
        # 未声明批量写能力的 Driver（如 Modbus）在任何输入下都明确拒绝，
        # 且在连接恢复之前拒绝，避免无意义的重连；绝不降级为逐点写入。
        if ProtocolCapability.WRITE_MANY not in self._driver.capabilities():
            raise NotImplementedError(
                f"{type(self._driver).__name__} does not support write_many"
            )
        if not writes:
            return ()
        await self._restore_if_needed()
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
        await self._restore_if_needed()
        return await self._driver.subscribe(point_ids, callback, interval=interval)

    async def interrogate(self) -> None:
        await self._restore_if_needed()
        await self._bounded(self._driver.interrogate(), self._settings.read_timeout, "interrogate")

    async def request_read_one(self, point_id: str) -> None:
        await self._restore_if_needed()
        method = getattr(self._driver, "request_read_one")  # noqa: B009 - optional IEC104 API
        await self._bounded(method(point_id), self._settings.read_timeout, "active read")

    async def request_read_many(self, point_ids: Sequence[str]) -> None:
        for point_id in point_ids:
            await self.request_read_one(point_id)

    async def read_active_one(
        self, point_id: str, *, timeout: float | None = None
    ) -> ProtocolSample:
        """IEC104-specific fresh read. Never substitute the local mirror."""
        await self._restore_if_needed()
        method = getattr(self._driver, "read_active_one")  # noqa: B009 - optional IEC104 API
        limit = self._settings.read_timeout if timeout is None else timeout
        return await self._bounded(method(point_id, timeout=limit), limit, "active read")

    async def read_active_many(
        self, point_ids: Sequence[str], *, timeout: float | None = None
    ) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        await self._restore_if_needed()
        method = getattr(self._driver, "read_active_many")  # noqa: B009 - optional IEC104 API
        limit = self._settings.read_timeout if timeout is None else timeout
        return await self._bounded(
            method(point_ids, timeout=limit), limit, "active read"
        )
