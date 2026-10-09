"""Shared protocol read/write recovery boundary.

A write is retried ONLY before its first transmission. Once dispatched,
timeouts/disconnects are ambiguous and NEVER trigger automatic retransmission.
"""

from __future__ import annotations  # noqa: I001 - imports follow application layering

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from core.domain import PointTable

from .errors import ProtocolError
from .port import ProtocolPort, ProtocolSampleCallback, SubscriptionHandle
from .protocol_contract import (
    ConnectionHealth,
    PointScalar,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class RecoverySettings:
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

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return self._driver.capabilities()

    def health(self) -> ConnectionHealth:
        return self._driver.health()

    def update_point_table(self, point_table: PointTable) -> None:
        self._driver.update_point_table(point_table)

    async def connect(self) -> None:
        await self._bounded(self._driver.connect(), self._settings.connect_timeout, "connect")

    async def close(self) -> None:
        await self._driver.close()

    async def _bounded(self, op: Awaitable[_T], limit: float | None, label: str) -> _T:
        try:
            return await asyncio.wait_for(op, timeout=limit)
        except TimeoutError as exc:
            raise ProtocolError(f"{label} timed out after {limit}s") from exc

    async def _restore_if_needed(self) -> None:
        if self.health().healthy:
            return
        async with self._connect_lock:
            if self.health().healthy:
                return
            last_error: Exception | None = None
            for _ in range(self._settings.reconnect_attempts):
                try:
                    # Explicitly dispose stale sockets/session handles.
                    await self._bounded(
                        self._driver.close(), self._settings.connect_timeout, "close"
                    )
                    await self.connect()
                    if self.health().healthy:
                        return
                    last_error = ProtocolError("connection not healthy after connect")
                except Exception as exc:
                    last_error = exc
            raise ProtocolError(
                f"connection unavailable after {self._settings.reconnect_attempts} "
                f"reconnect attempt(s): {last_error}"
            ) from last_error

    async def _read_with_recovery(self, action: Callable[[], Awaitable[_T]]) -> _T:
        await self._restore_if_needed()
        try:
            return await self._bounded(action(), self._settings.read_timeout, "read")
        except (ProtocolError, TimeoutError):
            # Reads are idempotent. Retry only if the Driver declares itself
            # disconnected, never on application/point-level errors.
            if self.health().healthy:
                raise
            await self._restore_if_needed()
            return await self._bounded(action(), self._settings.read_timeout, "read")

    async def read_one(self, point_id: str) -> ProtocolSample:
        return await self._read_with_recovery(lambda: self._driver.read_one(point_id))

    async def read_many(
        self, point_ids: Sequence[str]
    ) -> tuple[ProtocolSample, ...]:
        if not point_ids:
            return ()
        return await self._read_with_recovery(lambda: self._driver.read_many(point_ids))

    async def read(self, point_ids: Sequence[str]) -> tuple[ProtocolSample, ...]:
        return await self.read_many(point_ids)

    async def read_raw(
        self, point_ids: Sequence[str]
    ) -> tuple[tuple[PointScalar, Quality], ...]:
        if not point_ids:
            return ()
        raw_reader = getattr(self._driver, "read_raw", None)
        if raw_reader is not None:
            return await self._read_with_recovery(lambda: raw_reader(point_ids))
        samples = await self.read_many(point_ids)
        return tuple((s.value, s.quality) for s in samples)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        await self._restore_if_needed()
        # No retry after a write has begun: PLC/RTU may have applied the command.
        return await self._bounded(
            self._driver.write_one(write), self._settings.write_timeout, "write"
        )

    async def write_many(
        self, writes: Sequence[ProtocolWrite]
    ) -> tuple[ProtocolWriteResult, ...]:
        if not writes:
            return ()
        await self._restore_if_needed()
        # No retry after a write has begun: PLC/RTU may have applied the command.
        # A driver that does not support batch writes raises NotImplementedError,
        # which propagates unchanged and is never downgraded to per-point writes.
        return await self._bounded(
            self._driver.write_many(writes), self._settings.write_timeout, "write"
        )

    async def write(
        self, writes: Sequence[ProtocolWrite]
    ) -> tuple[ProtocolWriteResult, ...]:
        return await self.write_many(writes)

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
