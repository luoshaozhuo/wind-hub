"""一次性 Modbus TCP 会话；由 PyModbus 负责 TID 与收包关联。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pymodbus.client import AsyncModbusTcpClient

from core.application.errors import ProtocolConnectionError

from .config import ModbusConfig

_T = TypeVar("_T")


class _ModbusSession:
    """独占一个客户端；不对正常读写增加外部串行锁。"""

    def __init__(self, config: ModbusConfig) -> None:
        self._config = config
        self._client = AsyncModbusTcpClient(
            config.host,
            port=config.port,
            timeout=config.timeout,
            retries=0,
            reconnect_delay=0.0,
        )
        self._closing = False
        self._closed = False
        self._active: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return not self._closing and self._client.connected

    @property
    def closed(self) -> bool:
        return self._closed

    async def open(self) -> None:
        async def connect_client() -> None:
            if not await self._client.connect():
                raise ProtocolConnectionError("Modbus TCP connection refused")

        task = asyncio.create_task(connect_client())
        self._active.add(task)

        def settle(done: asyncio.Task[None]) -> None:
            self._active.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settle)
        try:
            await asyncio.shield(task)
        except BaseException:
            # 建链被取消后仍由 Session 追踪真实结果，随后再关闭资源。
            self.invalidate()
            await asyncio.shield(self._start_close())
            raise

    async def execute(self, request: Callable[[AsyncModbusTcpClient], Awaitable[_T]]) -> _T:
        if not self.connected:
            raise ProtocolConnectionError("Modbus session is not connected")
        # Task 保存底层事务所有权；取消调用方不会误认为网络请求已停止。
        async def invoke() -> _T:
            # 任务排队期间发生 close/cancel 时，不允许迟到提交请求。
            if not self.connected:
                raise ProtocolConnectionError("Modbus session closed before submission")
            return await request(self._client)

        task = asyncio.create_task(invoke())
        self._active.add(task)

        def settle(_: asyncio.Future[_T]) -> None:
            self._active.discard(task)
            if not task.cancelled():
                task.exception()

        task.add_done_callback(settle)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 请求可能已提交：旧会话不可复用，禁止自动重试。
            self.invalidate()
            raise
        except Exception as exc:
            # 业务异常响应以响应对象返回；没有收到可信响应属于连接错误。
            self.invalidate()
            raise ProtocolConnectionError(f"Modbus transaction failed: {exc}") from exc

    def invalidate(self) -> None:
        self._closing = True
        self._start_close()

    def _start_close(self) -> asyncio.Task[None]:
        if self._close_task is None:
            task = asyncio.create_task(self._close())

            def settle(done: asyncio.Task[None]) -> None:
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(settle)
            self._close_task = task
        return self._close_task

    async def close(self) -> None:
        self._closing = True
        await asyncio.shield(self._start_close())

    async def _close(self) -> None:
        if self._closed:
            return
        self._closing = True
        # 原生 API 的在途调用由自身 Task 收敛；不提前丢失所有权。
        while self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)
        self._client.close()
        self._closed = True
