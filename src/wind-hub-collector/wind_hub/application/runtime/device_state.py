"""DeviceRuntimeState —— 每台设备的最小运行状态与重连节流。

架构位置：application/runtime。与 :class:`DeviceConfig` 严格分离——
配置是不可变快照（热重载整体替换），运行状态随采集/重连活动演进，
绝不写回配置模型。

状态机（刻意保持最小）：

- ``connected`` — 当前是否可用（最近一次 connect/read 成功）；
- ``consecutive_failures`` — 连续 connect 失败次数，驱动 backoff；
- ``next_retry_at`` — 单调时钟语义的重连节流点：未到该时刻
  :meth:`Runtime.ensure_connected` 直接返回 ``False``，不发起
  connect——避免 1 Hz 轮询打满重连（连接风暴）。

Backoff（无 jitter，与驱动内部 reconnect 退避同族）：

    T_k = min(T_max, T_0 · 2^k)，T_0 = 1 s，T_max = 30 s

连接或读取成功后 ``consecutive_failures`` 清零。
"""

from __future__ import annotations

from dataclasses import dataclass

RECONNECT_BACKOFF_INITIAL = 1.0
"""首次重连等待（秒）。"""

RECONNECT_BACKOFF_MAX = 30.0
"""重连等待上限（秒）。"""


def reconnect_delay(consecutive_failures: int) -> float:
    """计算连续连接失败后的重连等待时长。

    Args:
        consecutive_failures: 当前连续失败次数；小于 1 时按首次失败处理。

    Returns:
        指数退避后的等待秒数，不超过 RECONNECT_BACKOFF_MAX。
    """
    k = max(0, consecutive_failures - 1)
    return min(RECONNECT_BACKOFF_MAX, RECONNECT_BACKOFF_INITIAL * (2.0**k))


def _error_text(error: BaseException) -> str:
    """把异常压成一行可读错误文本；空消息异常（如裸 ``TimeoutError``）
    回退为类型名，保证 ``last_error`` 不为空字符串。"""
    return str(error) or type(error).__name__


@dataclass
class DeviceRuntimeState:
    """单台设备的运行状态（Runtime 持有，随采集/重连演进）。"""

    connected: bool = False
    """当前是否已连接（最近一次 connect / read 成功）。"""

    consecutive_failures: int = 0
    """连续 connect 失败次数——决定 backoff 档位，成功后清零。"""

    last_success_at: float | None = None
    """最近一次成功（connect 或 read）的单调时钟时间戳。"""

    last_failure_at: float | None = None
    """最近一次失败的单调时钟时间戳。"""

    next_retry_at: float = 0.0
    """允许下一次 connect 尝试的最早时刻（节流点）。"""

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
        """记录 connect 失败并推进重连退避窗口。

        Args:
            now: 单调时钟时间戳。
            error: 本次连接失败异常。
        """
        self.connected = False
        self.consecutive_failures += 1
        self.last_failure_at = now
        self.last_error = _error_text(error)
        self.next_retry_at = now + reconnect_delay(self.consecutive_failures)

    def mark_read_failure(
        self, now: float, error: BaseException, *, connection_level: bool
    ) -> None:
        """记录 read 失败。

        Args:
            now: 单调时钟时间戳。
            error: 本次读取失败异常。
            connection_level: 是否属于连接级失败；为 True 时额外标记断线，
                交由后续 ensure_connected 路径重连。
        """
        self.last_failure_at = now
        self.last_error = _error_text(error)
        if connection_level:
            self.connected = False
