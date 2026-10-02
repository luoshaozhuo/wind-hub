"""Commander 即时写命令分发器。

提供 command_id 进程内幂等、写超时、并发去重与设备路由；不承担权限、审计、
跨进程幂等或协议实现。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Mapping

from wind_hub_core.device.session import DeviceSession
from wind_hub_core.model.command import Command, CommandResult


class CommandDispatcher:
    """按设备路由即时写命令，并提供 LRU/TTL 幂等。"""

    def __init__(
        self,
        devices: Mapping[str, DeviceSession],
        *,
        default_timeout: float,
        idempotency_cache_size: int = 10000,
        idempotency_ttl: float = 3600.0,
    ) -> None:
        self._devices = devices
        self._default_timeout = default_timeout
        self._cache_max = idempotency_cache_size
        self._cache_ttl = idempotency_ttl
        self._cache: OrderedDict[str, tuple[CommandResult, float]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[CommandResult]] = {}

    def get_cached(self, command_id: str) -> CommandResult | None:
        """返回仍在 TTL 内的幂等结果。"""
        return self._cache_lookup(command_id, time.monotonic())

    async def send(self, command: Command) -> CommandResult:
        """执行单条写命令；相同 command_id 的并发请求共享一次真实写入。"""
        now = time.monotonic()
        cached = self._cache_lookup(command.command_id, now)
        if cached is not None:
            return cached

        task = self._inflight.get(command.command_id)
        if task is None:
            task = asyncio.create_task(self._execute(command))
            self._inflight[command.command_id] = task
            task.add_done_callback(
                lambda completed, command_id=command.command_id: self._finish(
                    command_id,
                    completed,
                )
            )
        return await asyncio.shield(task)

    async def send_batch(self, commands: list[Command]) -> list[CommandResult]:
        """并发执行多条命令并保持输入顺序。"""
        return list(await asyncio.gather(*(self.send(command) for command in commands)))

    async def _execute(self, command: Command) -> CommandResult:
        device = self._devices.get(command.device_id)
        if device is None:
            result = CommandResult(
                command_id=command.command_id,
                success=False,
                error=f"Unknown device '{command.device_id}'",
            )
            self._cache_store(command.command_id, result, time.monotonic())
            return result

        timeout = command.timeout if command.timeout > 0 else self._default_timeout
        try:
            results = await asyncio.wait_for(device.write([command]), timeout=timeout)
            result = (
                results[0]
                if results
                else CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error="DeviceSession.write returned empty list",
                )
            )
        except TimeoutError:
            result = CommandResult(
                command_id=command.command_id,
                success=False,
                error=f"write timeout after {timeout:.1f}s",
            )
        except Exception as exc:
            result = CommandResult(
                command_id=command.command_id,
                success=False,
                error=str(exc) or type(exc).__name__,
            )

        self._cache_store(command.command_id, result, time.monotonic())
        return result

    def _finish(self, command_id: str, task: asyncio.Task[CommandResult]) -> None:
        if self._inflight.get(command_id) is task:
            self._inflight.pop(command_id, None)

    def _cache_lookup(self, command_id: str, now: float) -> CommandResult | None:
        self._expire(now)
        item = self._cache.get(command_id)
        if item is None:
            return None
        result, _ = item
        self._cache.move_to_end(command_id)
        return result

    def _cache_store(self, command_id: str, result: CommandResult, now: float) -> None:
        self._cache.pop(command_id, None)
        while len(self._cache) >= self._cache_max:
            self._cache.popitem(last=False)
        self._cache[command_id] = (result, now)

    def _expire(self, now: float) -> None:
        expired = [
            command_id
            for command_id, (_result, timestamp) in self._cache.items()
            if now - timestamp > self._cache_ttl
        ]
        for command_id in expired:
            self._cache.pop(command_id, None)
