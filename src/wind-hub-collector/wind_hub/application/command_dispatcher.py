"""CommandDispatcher —— 指令分发、幂等与写超时（application 层）。

持有 ``dict[str, Device]``（与 Runtime 共享同一注册表），按
``Command.device_id`` 定位运行时 :class:`Device` 并委托 ``Device.write``
完成真实写入；本类不再感知 ``ProtocolPort``。

保留的职责：

- ``command_id`` 幂等（LRU + TTL，进程内）；
- 写超时（``Command.timeout > 0`` 优先，否则系统默认）；
- 异常 → ``CommandResult``（协议级失败内联，不上抛）；
- 成功/失败 metrics 回调；
- ``send_batch`` 并发下发。

``Command`` / ``CommandResult`` 领域模型仍在 ``domain.model.command``。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable

from wind_hub.application.runtime.device import Device
from wind_hub.domain.model.command import Command, CommandResult

logger = logging.getLogger(__name__)


class CommandDispatcher:
    """设备写指令分发器。

    职责：
    - 按 device_id 路由到 Runtime 当前 Device；
    - 以 command_id 提供进程内 LRU+TTL 幂等；
    - 对 Device.write 应用单命令超时；
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
        devices: dict[str, Device],
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
            相同 command_id 在 TTL 内直接返回缓存结果，且不会重复触发 metrics
            callback；这保证单进程内重试不会重复写设备。
        """
        # 1. 先查幂等缓存，命中即返回。
        now = time.monotonic()
        cached = self._cache_lookup(cmd.command_id, now)
        if cached is not None:
            logger.debug("Command '%s' hit idempotency cache", cmd.command_id)
            return cached

        # 2. 从当前 Runtime 注册表解析目标 Device。
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

        # 3. 执行设备写，并应用命令级/默认超时。
        # 优先级：Command.timeout > 0 用命令自带超时，否则用系统默认写超时。
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
                    error="Device.write returned empty list",
                )
            )
        except TimeoutError:
            # 错误语义区分操作阶段（write），不模糊成裸 "timeout"。
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

        # 4. 首次执行后记录计数并写入幂等缓存。
        # 缓存命中的提前返回不计数——首次执行时已计过，避免重复累计。
        if result.success:
            self._on_command_sent()
        else:
            self._on_command_failed()
        self._cache_store(cmd.command_id, result, now)
        return result

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
