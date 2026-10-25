"""一次性 ADS Connection 的异步资源管理与串行执行。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pyads  # type: ignore[import-untyped]

from core.application.errors import ProtocolError

logger = logging.getLogger(__name__)


class ADSCommandNotSentError(ProtocolError):
    """请求在提交到底层 ADS API 之前被拒绝。"""


class ADSResourceUncertainError(ProtocolError):
    """底层连接的资源释放状态无法确认。"""


class _AdsSession:
    """独占一个 pyads.Connection；整个生命周期只允许打开一次。"""

    def __init__(self, connection_factory: Callable[[], pyads.Connection]) -> None:
        self._connection = connection_factory()
        self._gate = asyncio.Lock()
        self._owners: set[asyncio.Task[Any]] = set()
        self._open_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._open_started = False
        self._opened = False
        self._closing = False
        self._closed = False
        self._uncertain = False
        self._open_failed = False

    @property
    def is_open(self) -> bool:
        """本地可提交命令，不代表远端 PLC 健康。"""
        return self._opened and not self._closing and not self._uncertain

    @property
    def is_settled(self) -> bool:
        """仅在物理关闭已确认完成时为真。"""
        return self._closed and not self._uncertain

    @property
    def is_uncertain(self) -> bool:
        return self._uncertain

    def _track(self, task: asyncio.Task[Any]) -> None:
        self._owners.add(task)
        task.add_done_callback(self._owners.discard)
        task.add_done_callback(self._consume_result)

    @staticmethod
    def _consume_result(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            logger.exception("Failed to inspect ADS background operation")

    @staticmethod
    async def _await_owner(task: asyncio.Task[Any], started: Callable[[], bool]) -> Any:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 未提交的排队命令应撤销，避免超时写入迟到下发。
            # 已提交的同步线程不可取消，由 owner 持有锁直至实际结束。
            if not started():
                task.cancel()
            raise

    def notification(self, datatype: object) -> Callable[..., Callable[..., None]]:
        """按 pyads 公共 API 为原生通知生成回调包装器。"""
        return self._connection.notification(datatype)

    async def open(self) -> None:
        """打开一次；调用方取消不使运行中的物理打开失去所有权。"""
        if self._open_started or self._closing:
            raise ProtocolError("ADS session cannot be opened again")
        self._open_started = True
        dispatched = False

        async def _run() -> None:
            nonlocal dispatched
            async with self._gate:
                if self._closing:
                    raise ADSCommandNotSentError("ADS session is closing")
                dispatched = True
                try:
                    await asyncio.to_thread(self._connection.open)
                except Exception:
                    # pyads 3.6.0: 同步 open() 失败时内部会回滚本地端口；
                    # 该结果明确，不等同于等待取消后的未知结果。
                    self._open_failed = True
                    raise
                except BaseException:
                    self._uncertain = True
                    raise
                self._opened = True

        task = asyncio.create_task(_run())
        self._open_task = task
        self._track(task)
        await self._await_owner(task, lambda: dispatched)

    async def execute(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """串行执行同步 pyads 方法，拒绝关闭后的新命令。"""
        if not self.is_open:
            raise ADSCommandNotSentError("ADS session is not available")
        dispatched = False

        async def _run() -> Any:
            nonlocal dispatched
            async with self._gate:
                if not self.is_open:
                    raise ADSCommandNotSentError("ADS session is closing")
                method = getattr(self._connection, operation)
                dispatched = True
                return await asyncio.to_thread(method, *args, **kwargs)

        task = asyncio.create_task(_run())
        self._track(task)
        return await self._await_owner(task, lambda: dispatched)

    async def close(self) -> None:
        """只提交一次物理关闭；重复调用共同等待同一关闭任务。"""
        if self._close_task is None:
            self._closing = True
            task = asyncio.create_task(self._close_once())
            self._close_task = task
            self._track(task)
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        async with self._gate:
            # gate 等待全部已提交物理 open/execute 真实退出。
            if self._opened:
                try:
                    await asyncio.to_thread(self._connection.close)
                except BaseException:
                    self._uncertain = True
                    raise
            if self._uncertain:
                raise ADSResourceUncertainError(
                    "ADS connection cleanup cannot be confirmed after partial open"
                )
            self._opened = False
            self._closed = True
