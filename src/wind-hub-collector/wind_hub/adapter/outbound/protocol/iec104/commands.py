"""IEC104 遥控命令的在途状态跟踪。

本模块维护 ACT → ACT_CON → ACT_TERM 的命令生命周期，并把协议确认结果解析为
CommandResult。每个 IOA 同一时刻只允许一个在途命令，避免响应无法关联。

超时调度由上层 Driver 负责；Registry 只负责注册、确认、终止和移除，不创建
网络连接，也不发送 ASDU。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from wind_hub.domain.model.command import Command, CommandResult
from wind_hub.domain.model.errors import CommandError

logger = logging.getLogger(__name__)


@dataclass
class PendingCommand:
    """等待从站响应的单条遥控请求状态。

    生命周期为：发送 ACT → 收到 ACT_CON → 收到 ACT_TERM → 完成；
    否定 ACT_CON 或上层超时会提前结束。
    """

    command: Command
    """原始领域 Command。"""

    ioa: int
    """目标 Information Object Address。"""

    future: asyncio.Future[CommandResult]
    """命令成功、失败或超时时由上层完成的 Future。"""

    created_at: float = field(default_factory=time.monotonic)
    """注册时的 monotonic 时间，用于超时语义。"""

    timeout: float = 30.0
    """完整遥控生命周期允许的最大等待时间，单位秒。"""

    activation_confirmed: bool = False
    """收到正向 ACT_CON 后为 True。"""

    negative_confirmation: bool = False
    """ACT_CON 的 P/N 位表示否定确认时为 True。"""

    def _build_result(self, success: bool, error: str | None = None) -> CommandResult:
        return CommandResult(
            command_id=self.command.command_id,
            success=success,
            error=error,
        )


class PendingCommandRegistry:
    """按 IOA 管理在途遥控请求。

    同一 IOA 同时最多允许一个 PendingCommand；这是响应能够稳定关联到原始
    Command 的前提。
    """

    def __init__(self) -> None:
        self._pending: dict[int, PendingCommand] = {}

    # ==================================================================
    # 注册
    # ==================================================================

    def register(self, pending: PendingCommand) -> None:
        """注册一个在途命令。

        Args:
            pending: 待注册 PendingCommand。

        Raises:
            CommandError: 同一 IOA 已存在未完成命令。
        """
        if pending.ioa in self._pending:
            existing = self._pending[pending.ioa]
            raise CommandError(
                f"IOA {pending.ioa} already has a pending command "
                f"'{existing.command.command_id}'",
                command_id=pending.command.command_id,
            )
        self._pending[pending.ioa] = pending
        logger.debug(
            "IEC104: registered pending command '%s' for IOA %d",
            pending.command.command_id,
            pending.ioa,
        )

    # ==================================================================
    # 协议生命周期回调
    # ==================================================================

    def on_activation_con(
        self,
        ioa: int,
        negative: bool,
    ) -> PendingCommand | None:
        """处理 ACT_CON。

        Args:
            ioa: 响应中的 IOA。
            negative: P/N 位是否表示否定确认。

        Returns:
            匹配的 PendingCommand；不存在匹配项时返回 None。

        Notes:
            否定确认会立即完成 Future 为失败；正向确认只标记
            activation_confirmed，仍等待 ACT_TERM。
        """
        pending = self._pending.get(ioa)
        if pending is None:
            logger.debug(
                "IEC104: ACT_CON for IOA %d — no matching pending command",
                ioa,
            )
            return None

        pending.negative_confirmation = negative
        if negative:
            logger.warning(
                "IEC104: negative confirmation for IOA %d (cmd '%s')",
                ioa,
                pending.command.command_id,
            )
            # 否定确认已明确失败，无需继续等待 ACT_TERM。
            if not pending.future.done():
                pending.future.set_result(
                    pending._build_result(
                        success=False,
                        error="negative confirmation",
                    )
                )
        else:
            pending.activation_confirmed = True
            logger.debug(
                "IEC104: activation confirmed for IOA %d (cmd '%s')",
                ioa,
                pending.command.command_id,
            )

        return pending

    def on_activation_term(self, ioa: int) -> PendingCommand | None:
        """处理 ACT_TERM 并完成命令生命周期。

        Args:
            ioa: 响应中的 IOA。

        Returns:
            被移除的 PendingCommand；不存在匹配项时返回 None。

        Notes:
            Future 尚未被否定确认完成时，将其置为成功。
        """
        pending = self._pending.pop(ioa, None)
        if pending is None:
            logger.debug(
                "IEC104: ACT_TERM for IOA %d — no matching pending command",
                ioa,
            )
            return None

        if not pending.future.done():
            pending.future.set_result(pending._build_result(success=True))
        logger.debug(
            "IEC104: activation terminated for IOA %d (cmd '%s')",
            ioa,
            pending.command.command_id,
        )

        return pending

    # ==================================================================
    # 清理
    # ==================================================================

    def remove(self, ioa: int) -> None:
        """移除指定 IOA 的在途命令；用于超时或发送失败清理。"""
        self._pending.pop(ioa, None)

    def get(self, ioa: int) -> PendingCommand | None:
        """返回指定 IOA 的在途命令；不存在时返回 None。"""
        return self._pending.get(ioa)

    def has_pending(self, ioa: int) -> bool:
        """判断指定 IOA 是否存在在途命令。"""
        return ioa in self._pending
