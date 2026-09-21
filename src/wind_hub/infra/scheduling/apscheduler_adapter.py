"""APScheduler 调度适配器——:class:`SchedulerPort` 的 asyncio 实现。

架构位置：infra 层。这是核心代码之外唯一允许依赖 APScheduler 类型的模块；
domain / application 只见 ``wind_hub.application.port.scheduling`` 的抽象。

实现要点：

- 底层使用 :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler`，与
  引擎共用同一事件循环，不引入额外线程；
- 周期 Job 以 ``max_instances=1`` 注册——同一 Job 的执行不并发重入
  （上一轮未完成时本轮不叠加）；
- ``misfire_grace_time=None`` + ``coalesce=True``——设备读超时等造成的
  延迟触发不被丢弃，而是合并为一次补跑，与旧设备循环「跑完再等下一轮」
  的容错语义对齐；
- 调度器实例在 :meth:`start` 时才创建：``AsyncIOScheduler`` 需要绑定运行中
  的事件循环，而组合根的 ``assemble()`` 是同步函数、无运行循环；
- ``JobMetadata`` 存放在适配器实例的 ``_job_metadata`` 注册表而非底层
  Job 上——APScheduler 3.x 的 ``Job`` 是 ``__slots__`` 类，无法挂载自定义
  属性，``kwargs`` 又是执行体调用参数。注册表随 Job 生命周期维护（注册
  写入、替换更新、移除删除），调度器 stop/start 不清空（实例级状态，
  Runtime 重启后会以相同 job_id 重新注册并覆盖）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# APScheduler 3.x 未附带 py.typed/stubs——与 pyads/aiokafka 同样按仓库惯例
# 逐导入豁免；类型安全由本模块对 SchedulerPort 的显式实现保证。
from apscheduler.job import Job  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from wind_hub.application.port.scheduling import JobFunc, JobInfo, JobMetadata


class APSchedulerAdapter:
    """``SchedulerPort`` 的 APScheduler 实现。

    生命周期：``start()`` 创建并启动底层调度器（幂等）；``stop()`` 关闭
    底层调度器并丢弃实例（不等待执行中的 Job——APScheduler 的
    ``shutdown(wait=False)`` 语义，执行中的协程由 Runtime 停机流程兜底）。
    """

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None
        # Job 业务元数据注册表——与 Job 同生命周期（见模块 docstring）。
        self._job_metadata: dict[str, JobMetadata] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """底层调度器是否已启动。"""
        return self._scheduler is not None and self._scheduler.running

    async def start(self) -> None:
        """创建（如需要）并启动底层调度器。幂等。"""
        if self._scheduler is None:
            self._scheduler = AsyncIOScheduler()
        if not self._scheduler.running:
            self._scheduler.start()

    async def stop(self) -> None:
        """停止底层调度器；未启动时为无操作。"""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    # ------------------------------------------------------------------
    # Job 管理
    # ------------------------------------------------------------------

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
        """注册固定间隔周期 Job；调度器未启动时先抛出明确错误。

        ``max_instances=1`` 保证同一 Job 不重入；``coalesce`` 与
        ``misfire_grace_time`` 的取值见模块 docstring。

        ``start_paused=True`` 时经 APScheduler 的 ``next_run_time=None``
        在注册点原子创建暂停态 Job（``Job.pause()`` 的底层语义即清空
        ``next_run_time``）——不是「注册运行后再暂停」的两步操作，注册到
        显式恢复之间不会触发任何执行。

        ``metadata`` 在底层注册成功后写入注册表（替换注册即覆盖更新）；
        注册失败（如同 id 冲突）不残留元数据。
        """
        scheduler = self._require_scheduler()
        extra: dict[str, Any] = {"next_run_time": None} if start_paused else {}
        scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            args=list(args),
            replace_existing=replace_existing,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,
            **extra,
        )
        self._job_metadata[job_id] = metadata

    def remove_job(self, job_id: str) -> None:
        """移除 Job 及其元数据；未知 id 抛 :class:`KeyError`。"""
        scheduler = self._require_scheduler()
        try:
            scheduler.remove_job(job_id)
        except Exception as exc:
            # APScheduler 对未知 id 抛 JobLookupError；端口契约统一为 KeyError。
            raise KeyError(job_id) from exc
        self._job_metadata.pop(job_id, None)

    def pause_job(self, job_id: str) -> None:
        """暂停 Job；未知 id 抛 :class:`KeyError`。"""
        self._get_apscheduler_job(job_id).pause()

    def resume_job(self, job_id: str) -> None:
        """恢复 Job；未知 id 抛 :class:`KeyError`。"""
        self._get_apscheduler_job(job_id).resume()

    async def trigger_job(self, job_id: str) -> None:
        """把 Job 的下次触发时间改为「现在」，由调度器立即执行一次。

        周期计划本身不变；暂停中的 Job 被触发后会恢复调度（APScheduler
        修改 ``next_run_time`` 即视为恢复）。
        """
        scheduler = self._require_scheduler()
        job = self._get_apscheduler_job(job_id)
        job.modify(next_run_time=datetime.now(scheduler.timezone))

    def get_job(self, job_id: str) -> JobInfo | None:
        """返回单个 Job 快照；不存在时返回 ``None``。"""
        if self._scheduler is None:
            return None
        job = self._scheduler.get_job(job_id)
        if job is None:
            return None
        return self._to_info(job)

    def list_jobs(self) -> list[JobInfo]:
        """返回全部 Job 快照；调度器未启动时为空列表。"""
        if self._scheduler is None:
            return []
        return [self._to_info(job) for job in self._scheduler.get_jobs()]

    # ------------------------------------------------------------------
    # 私有
    # ------------------------------------------------------------------

    def _require_scheduler(self) -> AsyncIOScheduler:
        """返回已创建的调度器；未 ``start`` 时抛错（Job 操作以启动为前提）。

        组合根与 Runtime 的顺序保证「先 start 后注册 Job」；此处的显式失败
        是为了让时序错误在测试期即可定位，而非静默堆积在内部状态里。
        """
        if self._scheduler is None:
            raise RuntimeError("scheduler is not started — call start() before managing jobs")
        return self._scheduler

    def _get_apscheduler_job(self, job_id: str) -> Job:
        """按 id 取底层 Job；未知 id 统一转换为端口契约的 ``KeyError``。"""
        scheduler = self._require_scheduler()
        job = scheduler.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def _to_info(self, job: Job) -> JobInfo:
        """把底层 Job 投影为端口层 ``JobInfo``（暂停 = 无下次触发时间）。

        元数据直接索引注册表——凡经 ``add_interval_job`` 注册的 Job 必有
        对应条目；缺失即注册表维护缺陷，让它显式暴露而非静默兜底。
        """
        trigger = job.trigger
        interval = (
            trigger.interval.total_seconds() if isinstance(trigger, IntervalTrigger) else None
        )
        return JobInfo(
            job_id=job.id,
            metadata=self._job_metadata[job.id],
            next_run_time=job.next_run_time,
            paused=job.next_run_time is None,
            interval_seconds=interval,
        )
