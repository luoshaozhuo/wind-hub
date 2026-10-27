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

    async def open(self) -> None:
        try:
            if not await self._client.connect():
                raise ProtocolConnectionError("Modbus TCP connection refused")
        except BaseException:
            self._client.close()
            self._closed = True
            raise

    async def execute(self, request: Callable[[AsyncModbusTcpClient], Awaitable[_T]]) -> _T:
        if not self.connected:
            raise ProtocolConnectionError("Modbus session is not connected")
        # Task 保存底层事务所有权；取消调用方不会误认为网络请求已停止。
        task = asyncio.create_task(request(self._client))
        self._active.add(task)
        task.add_done_callback(self._active.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # 请求可能已提交：旧会话不可复用，禁止自动重试。
            self.invalidate()
            raise
        except Exception:
            # 协议异常响应由 Driver 判定；库异常可能意味着响应未到达。
            self.invalidate()
            raise

    def invalidate(self) -> None:
        self._closing = True
        self._start_close()

    def _start_close(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
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
