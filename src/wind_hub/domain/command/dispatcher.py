"""Dispatcher — command routing, idempotency, and timeout handling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable

from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.port.outbound import ProtocolPort

logger = logging.getLogger(__name__)


class Dispatcher:
    """Command-dispatch engine (Domain component).

    Responsibilities:
      - Route a ``Command`` to the correct ``ProtocolPort``.
      - Idempotency: same ``command_id`` executes only once.
      - Timeout: wrap ``ProtocolPort.write`` in ``asyncio.wait_for``.

    Non-responsibilities:
      - Protocol implementation (delegated to ``ProtocolPort``).
      - Permission checks / audit (delegated to application services above).

    The idempotency cache combines LRU eviction with TTL expiry and
    lives entirely in-process.  Cross-process idempotency belongs to a
    higher layer (e.g. the SCADA master station).
    """

    def __init__(
        self,
        protocols: dict[str, ProtocolPort],
        idempotency_cache_size: int = 10000,
        idempotency_ttl: float = 3600.0,
        default_timeout: float = 5.0,
        on_command_sent: Callable[[], None] | None = None,
        on_command_failed: Callable[[], None] | None = None,
    ) -> None:
        self._protocols = protocols
        self._cache_max = idempotency_cache_size
        self._cache_ttl = idempotency_ttl
        # 命令未自带超时（``Command.timeout <= 0``）时使用的系统默认写超时，
        # 由组合根从 ``system.yaml`` 的 ``scheduler.write_timeout`` 注入。
        self._default_timeout = default_timeout
        # 命令成功/失败回调（决策 6）：组合根注入 Prometheus 计数器递增；
        # domain 不得依赖 infra（import-linter），故不在此直接 import metrics。
        self._on_command_sent = on_command_sent or (lambda: None)
        self._on_command_failed = on_command_failed or (lambda: None)
        # OrderedDict gives us LRU: most-recently-accessed item at the end.
        self._cache: OrderedDict[str, tuple[CommandResult, float]] = OrderedDict()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def send(self, cmd: Command) -> CommandResult:
        """Send a single command.

        1. Check idempotency cache — return cached result if present
           (cache hits are *not* re-counted in the sent/failed metrics).
        2. Look up the device's ``ProtocolPort`` — fail if unknown.
        3. Wrap ``ProtocolPort.write([cmd])`` with ``asyncio.wait_for``.
        4. Fire the sent/failed callback, cache the result, and return it.
        """
        # --- step 1: idempotency check ----------------------------------
        now = time.monotonic()
        cached = self._cache_lookup(cmd.command_id, now)
        if cached is not None:
            logger.debug("Command '%s' hit idempotency cache", cmd.command_id)
            return cached

        # --- step 2: resolve target device ------------------------------
        proto = self._protocols.get(cmd.device_id)
        if proto is None:
            result = CommandResult(
                command_id=cmd.command_id,
                success=False,
                error=f"Unknown device '{cmd.device_id}'",
            )
            self._on_command_failed()
            self._cache_store(cmd.command_id, result, now)
            return result

        # --- step 3: write with timeout ----------------------------------
        # 优先级：Command.timeout > 0 用命令自带超时，否则用系统默认写超时。
        timeout = cmd.timeout if cmd.timeout > 0 else self._default_timeout
        try:
            results = await asyncio.wait_for(
                proto.write([cmd]),
                timeout=timeout,
            )
            result = (
                results[0]
                if results
                else CommandResult(
                    command_id=cmd.command_id,
                    success=False,
                    error="ProtocolPort.write returned empty list",
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

        # --- step 4: metrics, cache & return -----------------------------
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
        """Send multiple commands concurrently.

        Each command is processed independently (including idempotency
        checks).  Result list order matches *cmds* order.
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
        """Clear the idempotency cache (for tests / maintenance)."""
        self._cache.clear()

    @property
    def cache_size(self) -> int:
        """Current number of entries in the idempotency cache."""
        return len(self._cache)

    # ------------------------------------------------------------------
    # cache helpers (LRU + TTL)
    # ------------------------------------------------------------------

    def _cache_expire(self, now: float) -> None:
        """Remove all entries whose TTL has elapsed."""
        expired = [k for k, (_result, ts) in self._cache.items() if now - ts > self._cache_ttl]
        for k in expired:
            del self._cache[k]

    def _cache_lookup(self, key: str, now: float) -> CommandResult | None:
        """Return cached result if present and not expired, else None.

        Also promotes the entry to most-recently-used (OrderedDict end).
        """
        self._cache_expire(now)
        entry = self._cache.get(key)
        if entry is None:
            return None
        _result, ts = entry
        if now - ts > self._cache_ttl:
            del self._cache[key]
            return None
        # Promote to MRU
        self._cache.move_to_end(key)
        return _result

    def _cache_store(self, key: str, result: CommandResult, now: float) -> None:
        """Store a result in the cache, evicting LRU if over capacity."""
        # If key already exists, remove first (it'll be re-added at MRU end)
        self._cache.pop(key, None)
        while len(self._cache) >= self._cache_max:
            self._cache.popitem(last=False)  # evict LRU
        self._cache[key] = (result, now)
