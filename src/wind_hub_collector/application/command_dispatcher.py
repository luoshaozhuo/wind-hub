"""CommandDispatcher —— 指令分发、幂等与写超时（application 层）。

持有 ``Mapping[str, DeviceSession]``（与 Runtime 共享同一注册表），按
``Command.device_id`` 定位运行时 :class:`~wind_hub_core.device.session.DeviceSession`
并委托 ``DeviceSession.write`` 完成真实写入；本类不再感知 ``ProtocolPort``。

保留的职责：

- ``command_id`` 幂等（in-flight 去重 + LRU/TTL 结果缓存，进程内）；
- 写超时（``Command.timeout > 0`` 优先，否则系统默认）；
- 异常 → ``CommandResult``（协议级失败内联，不上抛）；
- 成功/失败 metrics 回调；
- ``send_batch`` 并发下发。

``Command`` / ``CommandResult`` 来自 wind-hub-core 公共领域模型。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping

from wind_hub_core.device.session import DeviceSession
from wind_hub_core.model.command import Command, CommandResult

logger = logging.getLogger(__name__)


class CommandDispatcher:
    """设备写指令分发器。

    职责：
    - 按 device_id 路由到 当前 DeviceSession；
    - 以 command_id 提供进程内 LRU+TTL 幂等；
    - 对 DeviceSession.write 应用单命令超时；
    - 把协议/设备异常收敛为 CommandResult；
    - 通过注入 callback 上报成功/失败计数。

    不负责权限、审计、跨进程幂等或协议实现。跨进程幂等应由更高层控制面保证。

    Args:
        devices: 与 Runtime 共享的设备注册表。
        idempotency_cache_size: 幂等缓存最大条目数。
        idempotency_ttl: 幂等结果保留时长，单位秒。
        default_timeout: Command 未提供正超时时使用的默认写超时。
        on_command_sent: 首次成功执行后的回调。
        on_command_failed: 首次失败执行后的回调。
    """

    def __init__(
        self,
        devices: Mapping[str, DeviceSession],
        idempotency_cache_size: int = 10000,
        idempotency_ttl: float = 3600.0,
        default_timeout: float = 5.0,
        on_command_sent: Callable[[], None] | None = None,
        on_command_failed: Callable[[], None] | None = None,
    ) -> None:
        self._devices = devices
        self._cache_max = idempotency_cache_size
        self._cache_ttl = idempotency_ttl
        # 命令未自带超时（``Command.timeout <= 0``）时使用的系统默认写超时，
        # 由组合根从 ``system.yaml`` 的 ``runtime.write_timeout`` 注入。
        self._default_timeout = default_timeout
        # 命令成功/失败回调：组合根注入 Prometheus 计数器递增；
        # application 不得依赖 infra，故不在此直接 import metrics。
        self._on_command_sent = on_command_sent or (lambda: None)
        self._on_command_failed = on_command_failed or (lambda: None)
        # OrderedDict 尾部保存最近访问项，用于 O(1) LRU 淘汰。
        self._cache: OrderedDict[str, tuple[CommandResult, float]] = OrderedDict()
        # 同一 command_id 的并发请求共享一个执行 Task，避免重复写设备。
        self._inflight: dict[str, asyncio.Task[CommandResult]] = {}

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def send(self, cmd: Command) -> CommandResult:
        """执行单条写指令。

        Args:
            cmd: 待执行 Command。

        Returns:
            CommandResult。未知设备、写超时和底层异常均内联为失败结果，不向上抛。

        Notes:
            相同 command_id 在 TTL 内直接返回缓存结果；若首个请求仍在执行，
            后续并发请求等待同一个 in-flight Task。等待方被取消不会取消真实写入。
        """
        now = time.monotonic()
        cached = self._cache_lookup(cmd.command_id, now)
        if cached is not None:
            logger.debug("Command '%s' hit idempotency cache", cmd.command_id)
            return cached

        task = self._inflight.get(cmd.command_id)
        if task is None:
            task = asyncio.create_task(self._execute(cmd))
            self._inflight[cmd.command_id] = task
            command_id = cmd.command_id

            def _on_done(completed: asyncio.Task[CommandResult]) -> None:
                self._finish_inflight(command_id, completed)

            task.add_done_callback(_on_done)
        else:
            logger.debug("Command '%s' joined in-flight execution", cmd.command_id)

        return await asyncio.shield(task)

    async def _execute(self, cmd: Command) -> CommandResult:
        """执行一次真实写入，并把最终结果写入幂等缓存。"""
        now = time.monotonic()

        device = self._devices.get(cmd.device_id)
        if device is None:
            result = CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=f"Unknown device '{cmd.device_id}'",
            )
            self._on_command_failed()
            self._cache_store(cmd.command_id, result, now)
            return result

        timeout = cmd.timeout if cmd.timeout > 0 else self._default_timeout
        try:
            results = await asyncio.wait_for(
                device.write([cmd]),
                timeout=timeout,
            )
            result = (
                results[0]
                if results
                else CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error="DeviceSession.write returned empty list",
                )
            )
        except TimeoutError:
            logger.warning(
                "write timeout: device=%s point=%s timeout=%.1fs",
                cmd.device_id,
                cmd.point_id,
                timeout,
            )
            result = CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=f"write timeout after {timeout:.1f}s",
            )
        except Exception as exc:
            logger.warning(
                "Command '%s' failed: %s",
                cmd.command_id,
                exc,
                exc_info=True,
            )
            result = CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=str(exc),
            )

        if result.success:
            self._on_command_sent()
        else:
            self._on_command_failed()
        self._cache_store(cmd.command_id, result, now)
        return result

    def _finish_inflight(
        self,
        command_id: str,
        task: asyncio.Task[CommandResult],
    ) -> None:
        """仅移除当前 command_id 对应的已完成 Task。"""
        if self._inflight.get(command_id) is task:
            self._inflight.pop(command_id, None)

    async def send_batch(
        self,
        cmds: list[Command],
    ) -> list[CommandResult]:
        """并发执行多条命令。

        Args:
            cmds: 输入命令列表。

        Returns:
            与输入顺序一致的 CommandResult 列表。单命令异常被收敛为对应失败结果，
            不取消同批其他命令。
        """
        results: list[CommandResult | BaseException] = await asyncio.gather(
            *[self.send(c) for c in cmds],
            return_exceptions=True,
        )
        out: list[CommandResult] = []
        for i, res in enumerate(results):
            if isinstance(res, CommandResult):
                out.append(res)
            else:
                out.append(
                    CommandResult(
                        command_id=cmds[i].command_id,
                        success=False,
                        error=str(res),
                    )
                )
        return out

    def get_cached(self, command_id: str) -> CommandResult | None:
        """返回仍在 TTL 内的幂等结果；未命中时返回 None。

        该方法只读取进程内缓存，不执行设备 I/O。控制用例可在显式重连前
        查询它，保证重复 command_id 不因当前连接状态变化而改变结果。
        """
        return self._cache_lookup(command_id, time.monotonic())

    def clear_cache(self) -> None:
        """清空进程内幂等缓存；用于维护或测试隔离。"""
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        """返回当前幂等缓存条目数。"""
        return len(self._cache)

    # ------------------------------------------------------------------
    # 幂等缓存辅助函数（LRU + TTL）
    # ------------------------------------------------------------------

    def _cache_expire(self, now: float) -> None:
        """删除 TTL 已过期的缓存条目。"""
        expired = [k for k, (_result, ts) in self._cache.items() if now - ts > self._cache_ttl]
        for k in expired:
            del self._cache[k]

    def _cache_lookup(self, key: str, now: float) -> CommandResult | None:
        """查询有效缓存并提升为 MRU；不存在或过期时返回 None。"""
        self._cache_expire(now)
        entry = self._cache.get(key)
        if entry is None:
            return None
        _result, ts = entry
        if now - ts > self._cache_ttl:
            del self._cache[key]
            return None
        # 命中后提升为最近使用。
        self._cache.move_to_end(key)
        return _result

    def _cache_store(self, key: str, result: CommandResult, now: float) -> None:
        """写入结果；容量不足时先淘汰最久未使用条目。"""
        # 同 key 先删除再写入尾部，确保 MRU 顺序正确。
        self._cache.pop(key, None)
        while len(self._cache) >= self._cache_max:
            self._cache.popitem(last=False)  # 淘汰 LRU。
        self._cache[key] = (result, now)
