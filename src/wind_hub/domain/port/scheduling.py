"""调度端口——「什么时候执行哪个 Job」的抽象。

架构位置：domain 层 outbound 端口。核心代码（Runtime / JobService）只依赖
本模块定义的 :class:`SchedulerPort`，不直接依赖任何具体调度实现；APScheduler
适配器位于 ``wind_hub.infra.scheduling``，由组合根注入。

职责边界：本端口只表达 Job 的时间调度契约（注册周期 Job、暂停/恢复/移除/
立即触发、查询状态与调度器自身生命周期）。采集执行链、设备/Sink 生命周期、
热重载均不属于本端口——它们分别属于 ``domain.acquisition`` 与
``application.runtime``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel


class JobInfo(BaseModel):
    """调度 Job 的只读快照。

    ``next_run_time`` 为 ``None`` 表示 Job 处于暂停态（APScheduler 语义：
    暂停即清空下次触发时间），与 ``paused`` 字段保持一致。
    """

    job_id: str
    """Job 唯一标识（Runtime 约定为 ``poll:{device_id}:{group}``）。"""

    next_run_time: datetime | None
    """下一次计划触发时间（带时区）；暂停时为 ``None``。"""

    paused: bool
    """``True`` 表示 Job 已暂停，不会被调度触发。"""


# Job 执行体：无返回值的协程函数。参数经 ``args`` 透传，元素类型由调用方
# （Runtime）与执行体签名共同保证——这是面向调度库的动态边界，故用 ``Any``
# 并在注册点保持参数个数/顺序与执行体一致。
JobFunc = Callable[..., Awaitable[None]]


class SchedulerPort(Protocol):
    """周期任务调度端口——只负责「何时执行哪个 Job」。

    实现方必须保证：

    - 同一 ``job_id`` 的周期 Job 不并发重入（``max_instances=1`` 语义）；
    - Job 执行体抛出的异常不导致调度器停止（由实现方或执行体自身兜底）；
    - ``remove_job`` / ``pause_job`` / ``resume_job`` / ``trigger_job``
      对未知 ``job_id`` 抛 :class:`KeyError`，调用方据此区分「Job 不存在」。
    """

    @property
    def running(self) -> bool:
        """调度器是否已启动（``start`` 之后、``stop`` 之前）。"""
        ...

    async def start(self) -> None:
        """启动调度器主循环。幂等：重复调用为无操作。"""
        ...

    async def stop(self) -> None:
        """停止调度器——不再触发新 Job；不等待执行中的 Job（由 Job 自身
        响应取消/停机语义）。幂等。"""
        ...

    def add_interval_job(
        self,
        job_id: str,
        interval_seconds: float,
        func: JobFunc,
        args: tuple[Any, ...] = (),
        replace_existing: bool = False,
    ) -> None:
        """注册一个固定间隔周期 Job。

        Args:
            job_id: Job 唯一标识；重复注册且 ``replace_existing`` 为
                ``False`` 时行为由实现方决定（APScheduler 抛
                ``ConflictingIdError``），调用方应避免重复注册。
            interval_seconds: 触发间隔（秒）。
            func: Job 执行体（协程函数），每次触发以其返回值被忽略的方式
                调度执行；执行体自行处理内部异常。
            args: 透传给执行体的位置参数。
            replace_existing: ``True`` 时用新定义替换同 id 的既有 Job。
        """
        ...

    def remove_job(self, job_id: str) -> None:
        """移除 Job。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        ...

    def pause_job(self, job_id: str) -> None:
        """暂停 Job——保留定义但不再触发。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        ...

    def resume_job(self, job_id: str) -> None:
        """恢复已暂停的 Job，按周期重新参与调度。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        ...

    async def trigger_job(self, job_id: str) -> None:
        """立即触发一次 Job（不等下个周期点）；周期计划本身不变。

        Raises:
            KeyError: ``job_id`` 不存在。
        """
        ...

    def get_job(self, job_id: str) -> JobInfo | None:
        """返回单个 Job 的快照；不存在时返回 ``None``（区别于操作的
        ``KeyError``——查询语义允许缺席）。"""
        ...

    def list_jobs(self) -> list[JobInfo]:
        """返回当前全部 Job 的快照（顺序由实现方决定）。"""
        ...
