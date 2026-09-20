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
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel


class JobState(str, Enum):
    """调度 Job 的生命周期状态——只有两个取值。

    与采集执行状态（``AcquisitionRuntimeState`` 的 failed/partial）和设备
    连接状态（``DeviceRuntimeState`` 的 disconnected）严格分维度：采集失败
    或设备掉线不改变 Job 的调度状态。
    """

    RUNNING = "running"
    """周期调度处于启用状态——按 interval 正常触发。"""

    STOPPED = "stopped"
    """Job 已注册但处于暂停状态——保留定义，不会被调度触发。"""


#: 采集 Job 的分类标识（``JobMetadata.kind`` 取值）——Runtime 注册采集
#: Job 时写入，JobService 据此圈定批量启停范围。Job ID 只负责标识，
#: 分类只看 ``kind``。
POLL_JOB_KIND = "poll"


class JobMetadata(BaseModel):
    """Job 的业务元数据——与 Job ID 字符串解耦的显式分类/归属信息。

    由创建 Job 的业务层（Runtime）在注册时显式传入，调度实现方随 Job
    保存并在查询快照中原样返回；读取方（JobService）一律从本模型取
    kind/device/group，禁止反向解析 ``job_id`` 字符串。

    - 采集 Job：``JobMetadata(kind="poll", device_id=..., group=...)``；
    - 系统 Job（未来）：``JobMetadata(kind="system")``——无设备归属。
    """

    kind: str
    """Job 分类标识——批量操作与展示归类的唯一依据（如 ``"poll"``）。"""

    device_id: str | None = None
    """归属设备；非设备相关 Job（如系统 Job）为 ``None``。"""

    group: str | None = None
    """归属轮询分组；非采集 Job 为 ``None``。"""


class JobInfo(BaseModel):
    """调度 Job 的只读快照。

    ``next_run_time`` 为 ``None`` 表示 Job 处于暂停态（APScheduler 语义：
    暂停即清空下次触发时间），与 ``paused`` 字段保持一致。
    """

    job_id: str
    """Job 唯一标识（Runtime 约定为 ``poll:{device_id}:{group}``）。"""

    metadata: JobMetadata
    """业务元数据（分类与归属）——注册时由创建方显式传入，与 ``job_id``
    字符串解耦；查询方不得从 ``job_id`` 反推这些字段。"""

    next_run_time: datetime | None
    """下一次计划触发时间（带时区）；暂停时为 ``None``。"""

    paused: bool
    """``True`` 表示 Job 已暂停，不会被调度触发。"""

    interval_seconds: float | None = None
    """固定间隔周期 Job 的触发间隔（秒）；非周期触发器时为 ``None``。"""

    @property
    def state(self) -> JobState:
        """二态生命周期视图：暂停 → ``STOPPED``，否则 ``RUNNING``。"""
        return JobState.STOPPED if self.paused else JobState.RUNNING


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
      对未知 ``job_id`` 抛 :class:`KeyError`，调用方据此区分「Job 不存在」；
    - ``metadata`` 与 Job 同生命周期：注册时一并保存、``replace_existing``
      替换时更新、``remove_job`` 时清理；``pause_job``/``resume_job``
      不改变它。
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
        metadata: JobMetadata,
        args: tuple[Any, ...] = (),
        replace_existing: bool = False,
        start_paused: bool = False,
    ) -> None:
        """注册一个固定间隔周期 Job。

        Args:
            job_id: Job 唯一标识；重复注册且 ``replace_existing`` 为
                ``False`` 时行为由实现方决定（APScheduler 抛
                ``ConflictingIdError``），调用方应避免重复注册。
            interval_seconds: 触发间隔（秒）。
            func: Job 执行体（协程函数），每次触发以其返回值被忽略的方式
                调度执行；执行体自行处理内部异常。
            metadata: 业务元数据——由创建方显式传入；实现方随 Job 保存，
                在 ``get_job``/``list_jobs`` 快照中原样返回，不得从
                ``job_id`` 推导或缺省伪造。
            args: 透传给执行体的位置参数。
            replace_existing: ``True`` 时用新定义替换同 id 的既有 Job。
            start_paused: ``True`` 时 Job 注册即为暂停态
                （``next_run_time=None``，状态 ``STOPPED``）——实现方必须
                以原子方式注册（不得「先注册运行再暂停」），保证注册到
                恢复之间不会触发任何一次执行。恢复只能经显式
                :meth:`resume_job`。
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
