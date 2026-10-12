"""Collector 设备运行状态；周期恢复由 Core Watchdog 负责。"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import error_text as _error_text


@dataclass
class DeviceRuntimeState:
    """单台设备的运行状态（DeviceRuntime 持有，随采集/重连演进）。"""

    connected: bool = False
    """当前是否已连接（最近一次 connect / read 成功）。"""

    consecutive_failures: int = 0
    """连续连接失败次数，供运行指标使用。"""

    last_success_at: float | None = None
    """最近一次成功（connect 或 read）的单调时钟时间戳。"""

    last_failure_at: float | None = None
    """最近一次失败的单调时钟时间戳。"""

    last_error: str | None = None
    """最近一次失败的简要描述（面向 status 输出）。"""

    def mark_success(self, now: float) -> None:
        """记录 connect/read 成功并清零失败计数。

        Args:
            now: 单调时钟时间戳。
        """
        self.connected = True
        self.consecutive_failures = 0
        self.last_success_at = now
        self.last_error = None

    def mark_connect_failure(self, now: float, error: BaseException) -> None:
        """记录 connect 失败。

        Args:
            now: 单调时钟时间戳。
            error: 本次连接失败异常。
        """
        self.connected = False
        self.consecutive_failures += 1
        self.last_failure_at = now
        self.last_error = _error_text(error)

    def mark_read_failure(
        self, now: float, error: BaseException, *, connection_level: bool
    ) -> None:
        """记录 read 失败。

        Args:
            now: 单调时钟时间戳。
            error: 本次读取失败异常。
            connection_level: 是否属于连接级失败；为 True 时额外标记断线，
                由 Watchdog 周期检查并恢复连接。
        """
        self.last_failure_at = now
        self.last_error = _error_text(error)
        if connection_level:
            self.connected = False
