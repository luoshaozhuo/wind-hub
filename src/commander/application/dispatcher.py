"""Commander 即时写命令分发器。

提供 command_id 进程内幂等、写超时、并发去重、连接保证与设备路由；
不承担权限、审计、跨进程幂等或协议实现。

幂等语义（与旧 Commander 一致）：相同 command_id 只有在命令内容签名一致
时才允许复用结果/合并在途；同键不同内容返回幂等冲突失败。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict

from .command import Command, CommandResult
from .errors import CommandError
from .runtime import CommanderRuntime


class CommandDispatcher:
    """按设备路由即时写命令，并提供内容安全的 LRU/TTL 幂等。"""

    def __init__(
        self,
        runtime: CommanderRuntime,
        *,
        default_timeout: float,
        idempotency_cache_size: int = 10000,
        idempotency_ttl: float = 3600.0,
    ) -> None:
        self._runtime = runtime
        self._default_timeout = default_timeout
        self._cache_max = idempotency_cache_size
        self._cache_ttl = idempotency_ttl
        self._cache: OrderedDict[str, tuple[str, CommandResult, float]] = OrderedDict()
        self._inflight: dict[str, tuple[str, asyncio.Task[CommandResult]]] = {}

    async def send(self, command: Command) -> CommandResult:
        """执行单条写命令；相同幂等键只有在命令内容一致时才允许复用。"""
        signature = self._signature(command)
        now = time.monotonic()
        cached = self._cache_lookup(command.command_id, now)
        if cached is not None:
            cached_signature, result = cached
            if cached_signature != signature:
                return self._conflict(command.command_id)
            return result

        inflight = self._inflight.get(command.command_id)
        if inflight is not None:
            inflight_signature, task = inflight
            if inflight_signature != signature:
                return self._conflict(command.command_id)
            return await asyncio.shield(task)

        task = asyncio.create_task(self._execute(command, signature))
        self._inflight[command.command_id] = (signature, task)
        command_id = command.command_id

        def _on_done(completed: asyncio.Task[CommandResult]) -> None:
            self._finish(command_id, completed)

        task.add_done_callback(_on_done)
        return await asyncio.shield(task)

    async def _execute(self, command: Command, signature: str) -> CommandResult:
        """固定 generation，保证连接后执行一次真实写入并缓存最终结果。"""
        async with self._runtime.operation():
            try:
                device = self._runtime.device(command.device_id)
            except KeyError:
                result = CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error=f"unknown device '{command.device_id}'",
                )
                self._cache_store(command.command_id, signature, result, time.monotonic())
                return result

            if not await self._runtime.ensure_connected(command.device_id):
                result = CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error=f"device '{command.device_id}' is not connected",
                )
                self._cache_store(command.command_id, signature, result, time.monotonic())
                return result

            timeout = command.timeout if command.timeout > 0 else self._default_timeout
            try:
                await asyncio.wait_for(
                    device.write_point(command.point_id, command.value),
                    timeout=timeout,
                )
                result = CommandResult(command_id=command.command_id, success=True)
            except TimeoutError:
                result = CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error=f"write timeout after {timeout:.1f}s",
                )
            except CommandError as exc:
                result = CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error=str(exc),
                )
            except Exception as exc:
                result = CommandResult(
                    command_id=command.command_id,
                    success=False,
                    error=str(exc) or type(exc).__name__,
                )

        self._cache_store(command.command_id, signature, result, time.monotonic())
        return result

    @staticmethod
    def _signature(command: Command) -> str:
        """生成逻辑命令签名；issued_at/timeout 不改变写入内容身份。"""
        return json.dumps(
            {"device_id": command.device_id, "point_id": command.point_id, "value": command.value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _conflict(command_id: str) -> CommandResult:
        return CommandResult(
            command_id=command_id,
            success=False,
            error="idempotency conflict: command_id reused with different command payload",
        )

    def _finish(self, command_id: str, task: asyncio.Task[CommandResult]) -> None:
        item = self._inflight.get(command_id)
        if item is not None and item[1] is task:
            self._inflight.pop(command_id, None)

    def _cache_lookup(self, command_id: str, now: float) -> tuple[str, CommandResult] | None:
        self._expire(now)
        item = self._cache.get(command_id)
        if item is None:
            return None
        signature, result, _ = item
        self._cache.move_to_end(command_id)
        return signature, result

    def _cache_store(
        self, command_id: str, signature: str, result: CommandResult, now: float
    ) -> None:
        self._cache.pop(command_id, None)
        while len(self._cache) >= self._cache_max:
            self._cache.popitem(last=False)
        self._cache[command_id] = (signature, result, now)

    def _expire(self, now: float) -> None:
        expired = [
            command_id
            for command_id, (_signature, _result, timestamp) in self._cache.items()
            if now - timestamp > self._cache_ttl
        ]
        for command_id in expired:
            self._cache.pop(command_id, None)
