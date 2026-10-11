"""周期通信健康检查与一次性重连用例。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .protocol_contract import ConnectionHealth

logger = logging.getLogger(__name__)

HealthCheck = Callable[[], Awaitable[ConnectionHealth]]
LifecycleOperation = Callable[[], Awaitable[None]]
ReconnectCallback = Callable[[], Awaitable[None]]


class WatchdogUseCase:
    """周期检查一个通信端点，失败时至多执行一次重连。

    Watchdog 不解释协议或订阅业务，只依赖 health、close、connect。
    重连成功后的恢复工作由可选 on_success 回调负责。
    """

    def __init__(
        self,
        *,
        health: HealthCheck,
        close: LifecycleOperation,
        connect: LifecycleOperation,
        interval: float,
        health_timeout: float,
        connect_timeout: float,
        on_success: ReconnectCallback | None = None,
        lifecycle_lock: asyncio.Lock | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("watchdog interval must be > 0")
        if health_timeout <= 0:
            raise ValueError("watchdog health_timeout must be > 0")
        if connect_timeout <= 0:
            raise ValueError("watchdog connect_timeout must be > 0")

        self._health = health
        self._close = close
        self._connect = connect
        self._interval = float(interval)
        self._health_timeout = float(health_timeout)
        self._connect_timeout = float(connect_timeout)
        self._on_success = on_success
        self._lifecycle_lock = lifecycle_lock or asyncio.Lock()

        self._reconnect_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._enabled = True
        self._generation = 0

    def configure(
        self,
        *,
        interval: float,
        health_timeout: float,
        connect_timeout: float,
    ) -> None:
        """更新后续周期使用的时序参数。"""
        if interval <= 0 or health_timeout <= 0 or connect_timeout <= 0:
            raise ValueError("watchdog timing values must be > 0")
        self._interval = float(interval)
        self._health_timeout = float(health_timeout)
        self._connect_timeout = float(connect_timeout)
        self._wake.set()

    async def check_once(self) -> ConnectionHealth:
        """执行一次有界健康检查；需要时最多触发一次重连。"""
        generation = self._generation
        try:
            # asyncio.timeout 在当前任务内直接 await，避免 wait_for 的子任务
            # 在外层取消时未开始执行即被销毁（coroutine never awaited）。
            async with asyncio.timeout(self._health_timeout):
                health = await self._health()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            health = ConnectionHealth(
                healthy=False,
                message=f"health check failed: {exc}",
            )

        if health.healthy or not self._enabled:
            return health

        await self._reconnect_if_current(generation)
        return health

    async def reconnect(
        self,
        on_success: ReconnectCallback | None = None,
    ) -> None:
        """关闭旧连接并建立一次新连接，成功后执行可选外部回调。"""
        callback = self._on_success if on_success is None else on_success
        await self._reconnect(callback=callback, expected_generation=None)

    async def start(self) -> None:
        """启动周期任务；重复调用保持幂等。"""
        if self._task is not None and not self._task.done():
            return
        self._enabled = True
        self._wake.clear()
        self._task = asyncio.create_task(self._run(), name="communication-watchdog")

    async def stop(self) -> None:
        """停止后续检查；不会在停止后发起新的重连。"""
        self._enabled = False
        self._wake.set()
        task = self._task
        self._task = None
        if task is None:
            return
        # 停止不应被故障设备的在途健康检查/连接操作无限阻塞。
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

    async def _reconnect_if_current(self, generation: int) -> bool:
        return await self._reconnect(
            callback=self._on_success,
            expected_generation=generation,
        )

    async def _reconnect(
        self,
        *,
        callback: ReconnectCallback | None,
        expected_generation: int | None,
    ) -> bool:
        async with self._reconnect_lock:
            if not self._enabled:
                return False
            if expected_generation is not None and expected_generation != self._generation:
                return False

            async with self._lifecycle_lock:
                async with asyncio.timeout(self._connect_timeout):
                    await self._close()
                async with asyncio.timeout(self._connect_timeout):
                    await self._connect()
                self._generation += 1

        # 外部回调不得在生命周期锁内执行，避免回调重入造成死锁。
        if callback is not None and self._enabled:
            await callback()
        return True

    async def _run(self) -> None:
        while self._enabled:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("watchdog cycle failed", exc_info=True)

            if not self._enabled:
                break
            try:
                async with asyncio.timeout(self._interval):
                    await self._wake.wait()
            except TimeoutError:
                continue
            self._wake.clear()
