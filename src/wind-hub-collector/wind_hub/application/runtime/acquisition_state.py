"""AcquisitionRuntimeState —— 每个采集执行（Task Instance）的运行状态。

架构位置：application/runtime。与 :class:`DeviceRuntimeState` 严格分维度：
设备连接状态回答「设备通不通」，本模型回答「这个采集实例最近跑得怎样」。
实例生命周期状态（RUNNING / STOPPED，由 Runtime 显式簿记）是第三维度，
三者不可混淆：

- 实例生命周期 = 启停状态（STOPPED ≠ 设备 down）；
- ``DeviceRuntimeState`` = 连接状态（断线不删实例）；
- ``AcquisitionRuntimeState`` = 业务执行状态（最近一次成功/失败/耗时）。

粒度固定为 Task Instance（``{task_id}:{device_id}``）；不建逐点状态。
一次 ``collect`` 的生命周期：:meth:`begin` → :meth:`finish_success` /
:meth:`finish_failure`；无论哪条异常路径，``running`` 都必须在结束时
归位（引擎侧以 try/except 保证）。

所有时间戳为注入时钟（默认 ``time.monotonic``）语义，只做内部时长计算，
不面向人类可读输出。
"""

from __future__ import annotations

from dataclasses import dataclass


def _error_text(error: BaseException | str) -> str:
    """把异常/描述压成一行可读错误文本；空消息异常回退为类型名。"""
    if isinstance(error, str):
        return error
    return str(error) or type(error).__name__


@dataclass
class AcquisitionRuntimeState:
    """单个采集执行实例的运行状态（Runtime 持有，随 collect 演进）。"""

    instance_id: str
    """Task Instance 标识（``{task_id}:{device_id}``）。"""

    task_id: str
    device_id: str
    point_group: str

    running: bool = False
    """当前是否有一次 collect 正在执行。"""

    last_started_at: float | None = None
    """最近一次 collect 开始时刻（单调时钟）。"""

    last_finished_at: float | None = None
    """最近一次 collect 结束时刻（单调时钟）。"""

    last_success_at: float | None = None
    """最近一次成功（含 partial）完成时刻。"""

    last_duration: float | None = None
    """最近一次 collect 耗时（秒）。"""

    consecutive_failures: int = 0
    """连续失败次数——partial 不算失败；成功（含 partial）清零。"""

    last_error: str | None = None
    """最近一次失败的简要描述（面向 status 输出）。"""

    def begin(self, now: float) -> None:
        """记录一次 collect 开始。

        Args:
            now: 单调时钟时间戳。
        """
        self.running = True
        self.last_started_at = now

    def finish_success(self, now: float, *, partial: bool = False) -> None:
        """记录一次 collect 成功结束。

        Args:
            now: 单调时钟时间戳。
            partial: 批次是否含 BAD 点；partial 仍视为成功并清零连续失败。
        """
        self._finish(now)
        self.last_success_at = now
        self.consecutive_failures = 0
        self.last_error = None
        # partial 仅由指标层单独计数（acquisition_partial_total），
        # 状态层不累计——保持状态模型最小。

    def finish_failure(self, now: float, error: BaseException | str) -> None:
        """记录一次 collect 失败结束。

        Args:
            now: 单调时钟时间戳。
            error: 读异常、全部点 BAD 或断线跳过等失败原因。
        """
        self._finish(now)
        self.consecutive_failures += 1
        self.last_error = _error_text(error)

    def _finish(self, now: float) -> None:
        self.running = False
        self.last_finished_at = now
        if self.last_started_at is not None:
            self.last_duration = now - self.last_started_at
