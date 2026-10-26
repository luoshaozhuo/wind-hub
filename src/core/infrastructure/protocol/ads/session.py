"""Own a single pyads.Connection and serialize its blocking operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, cast

import pyads  # type: ignore[import-untyped]

from core.application.errors import ProtocolError

logger = logging.getLogger(__name__)


class ADSCommandNotSentError(ProtocolError):
    """The operation did not enter the native ADS call."""


class ADSResourceUncertainError(ProtocolError):
    """Native resource release cannot be confirmed."""


class _AdsSession:
    """One-use connection. A cancelled waiter never abandons native I/O."""

    def __init__(self, target_net_id: str, target_port: int, host: str) -> None:
        self._connection = pyads.Connection(target_net_id, target_port, host)
        self._gate = asyncio.Lock()
        self._pending: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._open_started = False
        self._opened = False
        self._closing = False
        self._closed = False
        self._uncertain = False

    @property
    def is_open(self) -> bool:
        return self._opened and not self._closing and not self._uncertain

    @property
    def is_settled(self) -> bool:
        return self._closed and not self._uncertain

    @property
    def is_uncertain(self) -> bool:
        return self._uncertain

    def notification(self, datatype: object) -> Callable[..., Callable[..., None]]:
        return cast("Callable[..., Callable[..., None]]", self._connection.notification(datatype))

    def _own(self, task: asyncio.Task[Any]) -> None:
        """Hold native work until it finishes, even if its caller goes away."""
        self._pending.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._pending.discard(done)
            if not done.cancelled():
                try:
                    done.exception()
                except Exception:
                    logger.exception("ADS task result could not be inspected")

        task.add_done_callback(finished)

    @staticmethod
    async def _wait(task: asyncio.Task[Any], started: Callable[[], bool]) -> Any:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Queued work can be cancelled; native work must run to completion.
            if not started():
                task.cancel()
            raise

    async def open(self) -> None:
        if self._open_started or self._closing:
            raise ProtocolError("ADS session can only open once")
        self._open_started = True
        started = False

        async def run() -> None:
            nonlocal started
            async with self._gate:
                if self._closing:
                    raise ADSCommandNotSentError("ADS session is closing")
                started = True
                try:
                    await asyncio.to_thread(self._connection.open)
                except BaseException:
                    # pyads may have opened its local port before reporting failure.
                    self._opened = self._connection.is_open
                    raise
                self._opened = True

        task = asyncio.create_task(run())
        self._own(task)
        await self._wait(task, lambda: started)

    async def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if not self.is_open:
            raise ADSCommandNotSentError("ADS session unavailable")
        started = False

        async def run() -> Any:
            nonlocal started
            async with self._gate:
                if not self.is_open:
                    raise ADSCommandNotSentError("ADS session is closing")
                method = getattr(self._connection, operation)
                started = True
                return await asyncio.to_thread(method, *args, **kwargs)

        task = asyncio.create_task(run())
        self._own(task)
        return await self._wait(task, lambda: started)

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_once())
            self._own(self._close_task)
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        async with self._gate:
            # All earlier native calls have left the single command gate.
            if self._opened:
                try:
                    await asyncio.to_thread(self._connection.close)
                except BaseException:
                    # Only a confirmed closed pyads connection is safe to release.
                    self._uncertain = self._connection.is_open
                    if self._uncertain:
                        raise
            if self._uncertain:
                raise ADSResourceUncertainError("ADS connection cleanup is unconfirmed")
            self._opened = False
            self._closed = True
